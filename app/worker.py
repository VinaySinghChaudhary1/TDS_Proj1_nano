"""
Background worker to process a single task.
Functions:
 - process_task(task_id) : loads TaskRecord from DB and runs the pipeline.
"""

import base64
import json
import logging
import time
from typing import Optional
from datetime import datetime
import requests

from .db import get_session
from . import models
from .llm_generator import generate_manifest
from .github_utils import (
    create_repo,
    create_or_update_file,
    create_or_update_binary_file,
    generate_mit_license,
    enable_pages_and_wait,
    _get_file_sha,
    GITHUB_API,
    _headers,
)
from .settings import settings

logger = logging.getLogger(__name__)


# -----------------------------------------------------------
# Helper: Extract selector for lightweight manifest validation
# -----------------------------------------------------------
def _simple_selector_from_check(check: str) -> Optional[str]:
    import re
    m = re.search(r"querySelector(All)?\(['\"](.+?)['\"]\)", check)
    if m:
        return m.group(2)
    m2 = re.search(r"getElementById\(['\"](.+?)['\"]\)", check)
    if m2:
        return f"#{m2.group(1)}"
    return None


# -----------------------------------------------------------
# Helper: Lightweight manifest validation
# -----------------------------------------------------------
def _validate_manifest_basic(manifest: dict, checks: list) -> (bool, str):
    # Build a list of candidate HTML contents to search (index.html first, then any other .html/.htm)
    files = manifest.get("files", [])
    html_candidates = []
    # prefer index.html/index.htm first
    for name in ("index.html", "index.htm"):
        for f in files:
            if f.get("path", "").lower() == name:
                html_candidates.append((name, f.get("content", "")))
                break

    # add any other HTML files
    for f in files:
        path = f.get("path", "")
        if path.lower().endswith('.html') or path.lower().endswith('.htm'):
            if not any(path.lower() == p.lower() for p, _ in html_candidates):
                html_candidates.append((path, f.get("content", "")))

    if not html_candidates:
        return False, "no HTML files found in manifest"

    # Parse all candidate HTML with BeautifulSoup where possible
    soups = []
    from bs4 import BeautifulSoup
    for path, content in html_candidates:
        try:
            soups.append((path, BeautifulSoup(content, "html.parser"), content))
        except Exception:
            soups.append((path, None, content))

    for check in checks:
        selector = _simple_selector_from_check(check)
        if not selector:
            continue
        found = False
        for path, soup, raw in soups:
            if soup:
                try:
                    res = soup.select(selector)
                    if res:
                        found = True
                        break
                except Exception:
                    pass
            # fallback: plain string search in raw content
            if selector in raw:
                found = True
                break
        if not found:
            return False, f"Selector {selector} from check not found in any HTML file (checked {', '.join(p for p,_,_ in soups)})"
    return True, "basic checks passed"


# -----------------------------------------------------------
# Upload attachments (improved downloader)
# -----------------------------------------------------------
def _push_attachments(owner: str, repo: str, attachments: list):
    """Push attachment files (binary or text) to GitHub repo with retries + fallback."""
    if not attachments:
        return
    for a in attachments:
        name = a.get("name")
        url = a.get("url")
        if not name or not url:
            continue
        try:
            logger.info("📎 Uploading attachment: %s", name)
            binary_data = b""
            if url.startswith("data:"):
                base64_data = url.split("base64,")[-1]
                binary_data = base64.b64decode(base64_data + "==", validate=False)
            else:
                headers = {"User-Agent": "Mozilla/5.0"}
                for attempt in range(2):
                    try:
                        r = requests.get(url, headers=headers, timeout=20)
                        r.raise_for_status()
                        binary_data = r.content
                        break
                    except Exception as e:
                        logger.warning("⚠️ Attempt %s failed for %s: %s", attempt + 1, name, e)
                        time.sleep(1)
                if not binary_data:
                    logger.warning("⚠️ Using placeholder for %s (download failed).", name)
                    binary_data = b""

            create_or_update_binary_file(owner, repo, name, binary_data, f"add attachment {name}")
            logger.info("✅ Uploaded attachment: %s", name)
        except Exception as e:
            logger.warning("⚠️ Failed to upload attachment %s: %s", name, e)


# -----------------------------------------------------------
# Helper: Push manifest files to GitHub
# -----------------------------------------------------------
def _push_manifest_to_github(manifest: dict, repo_name: str, commit_msg_prefix: str = "tds: generate") -> str:
    owner = settings.GITHUB_OWNER
    last_commit_sha = None

    logger.info("Ensuring repo exists: %s/%s", owner, repo_name)
    create_repo(repo_name, description="TDS generated repo", private=False)

    for f in manifest.get("files", []):
        path = f["path"]
        content = f.get("content", "")
        encoding = f.get("encoding", "utf-8")
        commit_msg = f"{commit_msg_prefix}: {path}"
        try:
            if encoding and encoding.lower() == "base64":
                raw = base64.b64decode(content)
                resp = create_or_update_binary_file(owner, repo_name, path, raw, commit_msg)
            else:
                resp = create_or_update_file(owner, repo_name, path, content, commit_msg)
            last_commit_sha = resp.get("commit", {}).get("sha") or last_commit_sha
        except Exception as exc:
            logger.exception("Failed to push file %s: %s", path, exc)
            raise

    try:
        logger.info("Adding MIT license to repository...")
        license_text = generate_mit_license()
        resp = create_or_update_file(owner, repo_name, "LICENSE", license_text, "chore: add MIT license")
        last_commit_sha = resp.get("commit", {}).get("sha") or last_commit_sha
        logger.info("✅ Successfully added LICENSE file")
    except Exception as exc:
        logger.warning("Failed to add LICENSE (non-critical): %s", str(exc))

    return last_commit_sha or ""


# -----------------------------------------------------------
# README.md updater (enhanced for full round details + note)
# -----------------------------------------------------------
def _update_readme(owner, repo_name, task, round_num, brief, checks, pages_url):
    """Create or append README.md with full round summaries for every round."""
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    sha = _get_file_sha(owner, repo_name, "README.md")

    render_note = ""
    if round_num >= 2:
        render_note = "\n> ⏳ **Note:** GitHub Pages may take around 10 minutes to fully render and reflect all updates for this round.\n"

    checks_section = "\n".join([f"- {c}" for c in checks or []])
    round_block = f"""
## {'🌀 Round 1' if round_num == 1 else f'🔁 Round {round_num} Update'} ({date_str})

**Brief:** {brief}

**Checks:**
{checks_section}

**Status:** ✅ {'Completed' if round_num == 1 else 'Redeployed'}

**Pages URL:** [{pages_url}]({pages_url})
{render_note}
---
"""

    header = f"# {task} — Task Report\n\n**GitHub Pages:** [View Site]({pages_url})\n\n---\n"

    if sha:
        # Read the current README
        url = f"{GITHUB_API}/repos/{owner}/{repo_name}/contents/README.md"
        r = requests.get(url, headers=_headers())
        current = ""
        if r.status_code == 200:
            import base64
            current = base64.b64decode(r.json()["content"]).decode("utf-8")

        new_content = current + "\n" + round_block
    else:
        new_content = header + round_block

    create_or_update_file(owner, repo_name, "README.md", new_content, f"update README for round {round_num}")


# -----------------------------------------------------------
# Notify evaluation endpoint
# -----------------------------------------------------------
def _notify_evaluation(evaluation_url: str, payload: dict, max_attempts: int = 6):
    if not evaluation_url:
        logger.warning("No evaluation_url provided; skipping notify")
        return False
    headers = {"Content-Type": "application/json"}
    attempt = 0
    wait = 1
    while attempt < max_attempts:
        try:
            r = requests.post(evaluation_url, json=payload, headers=headers, timeout=10)
            logger.info("Notify attempt %s -> status %s", attempt + 1, r.status_code)
            if 200 <= r.status_code < 300:
                return True
        except Exception as exc:
            logger.warning("Notify attempt %s failed: %s", attempt + 1, exc)
        attempt += 1
        time.sleep(wait)
        wait *= 2
    logger.error("All notify attempts failed for %s", evaluation_url)
    return False


# -----------------------------------------------------------
# MAIN WORKER FUNCTION
# -----------------------------------------------------------
def process_task(task_id: int):
    logger.info("Processing task id=%s", task_id)
    with next(get_session()) as session:
        task = session.get(models.TaskRecord, task_id)
        if not task:
            logger.error("Task id %s not found", task_id)
            return False

        task.status = "processing"
        session.add(task)
        session.commit()

        try:
            checks = json.loads(task.checks) if task.checks else []
        except Exception:
            checks = task.checks or []

        brief_with_seed = (task.brief or "").replace("${seed}", task.nonce)
        checks_with_seed = [c.replace("${seed}", task.nonce) if isinstance(c, str) else c for c in checks]

        logger.info("Replaced ${seed} with nonce '%s' in brief and checks", task.nonce)

        try:
            attachments = json.loads(task.attachments or "[]")
        except Exception:
            attachments = []

        try:
            manifest = generate_manifest(
                brief_with_seed, checks_with_seed, attachments=attachments, nonce=task.nonce, round_num=task.round
            )
        except Exception as exc:
            logger.exception("LLM manifest generation failed for task %s: %s", task_id, exc)
            task.status = "failed"
            session.add(task)
            session.commit()
            return False

        ok, msg = _validate_manifest_basic(manifest, checks_with_seed)
        if not ok:
            logger.error("Manifest validation failed: %s", msg)
            task.status = "failed"
            session.add(task)
            session.commit()
            return False

        safe_task = task.task.replace(" ", "-").lower()
        repo_name = f"{safe_task}-{task.nonce}"
        task.repo_name = repo_name
        session.add(task)
        session.commit()

        try:
            commit_sha = _push_manifest_to_github(manifest, repo_name)
            _push_attachments(settings.GITHUB_OWNER, repo_name, attachments)
            task.commit_sha = commit_sha
            session.add(task)
            session.commit()
        except Exception as exc:
            logger.exception("Failed to push manifest or attachments for task %s: %s", path, exc)
            task.status = "failed"
            session.add(task)
            session.commit()
            return False

        try:
            pages_url, _ = enable_pages_and_wait(settings.GITHUB_OWNER, repo_name, branch="main", path="/", timeout=180)
            task.pages_url = pages_url
            session.add(task)
            session.commit()
        except Exception as exc:
            logger.exception("Failed enabling pages for %s: %s", repo_name, exc)
            task.status = "failed"
            session.add(task)
            session.commit()
            return False

        try:
            _update_readme(settings.GITHUB_OWNER, repo_name, task.task, task.round, task.brief, checks, task.pages_url)
        except Exception as exc:
            logger.warning("Failed to update README for %s: %s", repo_name, exc)

        payload = {
            "email": task.email,
            "task": task.task,
            "round": task.round,
            "nonce": task.nonce,
            "repo_url": f"https://github.com/{settings.GITHUB_OWNER}/{repo_name}",
            "commit_sha": task.commit_sha,
            "pages_url": task.pages_url,
        }
        notified = _notify_evaluation(task.evaluation_url or "", payload)

        task.status = "done" if notified else "done_notify_failed"
        task.completed_at = datetime.utcnow()
        session.add(task)
        session.commit()

        logger.info("✅ Finished processing task id=%s round=%s", task_id, task.round)
        return True
