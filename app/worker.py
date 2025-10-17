
"""Upgraded worker.py
- Implements process_task(task_id) which:
  * loads a TaskRecord from DB (flexible to dict fallback)
  * runs three isolated stages: generate -> repo push -> notify
  * each stage has independent retries and logging
  * updates DB status fields where available
"""
import time
import logging
from typing import Optional, Tuple, Dict, Any

# Attempt to import project-specific modules; fall back gracefully
try:
    from .db import get_session, update_task_status, get_task_by_id
    from . import models
except Exception:
    # If project modules are not available as package imports, try relative
    try:
        from db import get_session, update_task_status, get_task_by_id
        import models
    except Exception:
        get_session = None
        update_task_status = None
        get_task_by_id = None
        models = None

# LLM and GitHub utilities
try:
    from .llm_generator import generate_app_from_brief, _validate_json_schema
except Exception:
    from llm_generator import generate_app_from_brief, _validate_json_schema

# GitHub helper functions - expected implementations in your repo. We will call
# create_repo_and_push(response, repo_name_hint) and it should return (repo_url, commit_sha, pages_url)
try:
    from .github_utils import create_repo_and_push
except Exception:
    create_repo_and_push = None  # will raise if not found

import json, requests, os, base64, mimetypes
from pathlib import Path
from datetime import datetime

def _safe_post(url: str, payload: Dict[str, Any], retries: int = 4, timeout: int = 15) -> bool:
    """Post JSON payload with exponential backoff.

    Returns True on success (status 200). Returns False after retries fail.
    Retries are attempted silently (logged at debug); final failure returns False
    instead of raising an exception to avoid crashing the worker.
    """
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.status_code == 200:
                return True
            logging.debug("POST to %s returned %s (attempt %s)", url, r.status_code, attempt)
        except Exception as e:
            logging.debug("POST to %s failed on attempt %s: %s", url, attempt, e)
        time.sleep(2 ** (attempt - 1))
    logging.error("Failed to POST to %s after %s attempts", url, retries)
    return False


def _update_db_status(task_obj, status: str, extra: Optional[Dict[str, Any]] = None):
    """Helper to update task status in DB or in-memory object."""
    try:
        if update_task_status and task_obj is not None:
            update_task_status(task_obj.get('id') if isinstance(task_obj, dict) else task_obj.id, status, extra or {})
        else:
            # If no DB helper exists, try to set attribute on object (best-effort)
            if isinstance(task_obj, dict):
                task_obj['status'] = status
            else:
                setattr(task_obj, 'status', status)
    except Exception as e:
        logging.debug("Could not update DB status: %s", e)


def _ensure_logs_dir() -> Path:
    p = Path('logs')
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        # best-effort
        pass
    return p


def _task_log(task_obj, message: str):
    """Append a timestamped message to the global tasks log and per-task log file."""
    try:
        task_id = task_obj.get('id') if isinstance(task_obj, dict) else getattr(task_obj, 'id', 'unknown')
        ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        line = f"{ts} - task:{task_id} - {message}\n"
        logs_dir = _ensure_logs_dir()
        # append to global log
        try:
            with open(logs_dir / 'tasks.log', 'a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception:
            logging.debug('Could not write to logs/tasks.log')
        # append to per-task log
        try:
            with open(logs_dir / f'task_{task_id}.log', 'a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception:
            logging.debug('Could not write to per-task log for %s', task_id)
    except Exception as e:
        logging.debug('Failed to write task log: %s', e)


def _stage_generate(task_brief: str, max_attempts: int = 3) -> Dict[str, Any]:
    """Stage 1: call LLM and validate output"""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            generated = generate_app_from_brief(task_brief, max_attempts=1)
            if _validate_json_schema(generated):
                return generated
            else:
                last_err = RuntimeError("Generated payload failed schema validation")
                logging.warning("Stage generate: invalid schema, attempt %s", attempt)
        except Exception as e:
            last_err = e
            logging.warning("Stage generate attempt %s failed: %s", attempt, e)
        time.sleep(2 ** (attempt - 1))
    raise last_err or RuntimeError("LLM generation stage failed")


def _download_with_retries(url: str, name: str, attempts: int = 3, timeout: int = 20):
    """Download a URL (http(s) or data:) with simple retries.

    Returns (content_bytes, content_type) or (None, None) on failure.
    """
    # data: URI handling
    if url.startswith('data:'):
        try:
            # format: data:[<mediatype>][;base64],<data>
            header, b64 = url.split(',', 1)
            is_base64 = header.endswith(';base64')
            ctype = header[5:].split(';')[0] if header.startswith('data:') else 'application/octet-stream'
            if is_base64:
                content = base64.b64decode(b64)
            else:
                content = b64.encode('utf-8')
            return content, ctype or None
        except Exception as e:
            logging.warning("Failed to decode data URI for %s: %s", name, e)
            return None, None

    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, stream=True, timeout=timeout)
            r.raise_for_status()
            content_type = r.headers.get('content-type')
            # read all content into memory (acceptable for small attachments)
            content = r.content
            return content, content_type
        except Exception as e:
            last_err = e
            logging.warning("Attempt %s failed downloading %s: %s", attempt, name, e)
            time.sleep(2 ** (attempt - 1))
    logging.warning("Download/decoding failed for %s", name)
    return None, None


def _process_attachments(task_obj: Dict[str, Any], generated: Dict[str, Any]) -> Dict[str, Any]:
    """Download attachments from task_obj['attachments'] and merge into generated['app_code'].

    - If generated['app_code'] is a string, convert it to a filename mapping with index.html.
    - Attachments with text-like content types (json, csv, text, html, md) will be stored as strings.
    - Binary attachments will be kept as bytes so the github helper can push binary files.
    Returns the updated generated dict.
    """
    attachments = []
    if isinstance(task_obj, dict):
        attachments = task_obj.get('attachments') or []

    if not attachments:
        return generated

    app_code = generated.get('app_code')
    if not isinstance(app_code, dict):
        # convert to dict, preserve original as index.html
        app_code = { 'index.html': str(app_code or '') }

    for att in attachments:
        try:
            name = att.get('name') or att.get('filename')
            url = att.get('url')
            if not name or not url:
                logging.debug('Skipping malformed attachment: %s', att)
                continue

            _task_log(task_obj, f"📎 Uploading attachment: {name}")
            content, ctype = _download_with_retries(url, name, attempts=3)
            if content is None:
                # fallback: write a small placeholder text file
                placeholder = f"Attachment {name} could not be downloaded."
                app_code[name] = placeholder
                _task_log(task_obj, f"⚠️ Placeholder used for attachment: {name}")
                continue

            # decide binary or text
            is_text = False
            if ctype:
                ctype_low = ctype.split(';')[0].lower()
                if ctype_low.startswith('text/') or ctype_low in ('application/json', 'application/javascript'):
                    is_text = True
            # fallback: infer from extension
            if not is_text:
                ext = os.path.splitext(name)[1].lower()
                if ext in ('.txt', '.md', '.html', '.csv', '.json', '.js'):
                    is_text = True

            if is_text:
                try:
                    text = content.decode('utf-8')
                except Exception:
                    text = content.decode('latin-1', errors='ignore')
                app_code[name] = text
            else:
                app_code[name] = content  # bytes

            _task_log(task_obj, f"✅ Uploaded attachment: {name}")
        except Exception as e:
            logging.warning('Failed processing attachment %s: %s', att, e)
            _task_log(task_obj, f"⚠️ Attachment failed: {att.get('name')} error={e}")

    generated['app_code'] = app_code
    return generated


def _stage_repo_push(generated_payload: Dict[str, Any], repo_hint: Optional[str] = None) -> Tuple[str, str, str]:
    """Stage 2: create repo and push files. Wrapped with retries for network/GitHub errors."""
    if create_repo_and_push is None:
        raise RuntimeError("create_repo_and_push is not implemented in github_utils")


    last_err = None
    for attempt in range(1, 5):
        try:
            repo_url, commit_sha, pages_url = create_repo_and_push(generated_payload, repo_hint)
            if repo_url and commit_sha and pages_url:
                return repo_url, commit_sha, pages_url
            else:
                last_err = RuntimeError("github_utils returned incomplete values")
                logging.warning("Repo push returned incomplete values, attempt %s", attempt)
        except Exception as e:
            last_err = e
            logging.warning("Repo push attempt %s failed: %s", attempt, e)
        time.sleep(2 ** (attempt - 1))
    raise last_err or RuntimeError("Repo push stage failed")


def _stage_notify_eval(task_obj: Any, repo_url: str, commit_sha: str, pages_url: str, evaluation_url: str) -> bool:
    """Stage 3: POST metadata to evaluation_url with retries.

    Returns True on success, False on failure (after retries).
    """
    payload = {
        "email": getattr(task_obj, 'email', task_obj.get('email') if isinstance(task_obj, dict) else None),
        "task": getattr(task_obj, 'task', task_obj.get('task') if isinstance(task_obj, dict) else None),
        "round": getattr(task_obj, 'round', task_obj.get('round') if isinstance(task_obj, dict) else None),
        "nonce": getattr(task_obj, 'nonce', task_obj.get('nonce') if isinstance(task_obj, dict) else None),
        "repo_url": repo_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url,
    }
    ok = _safe_post(evaluation_url, payload, retries=5)
    if not ok:
        logging.warning("Notification to evaluation_url failed after retries: %s", evaluation_url)
    return ok


def process_task(task_identifier):
    """Main entry point: accepts either a numeric task id or a dict-like task object.
    This function performs all stages and updates DB state.
    """
    logging.info("Starting process_task for %s", task_identifier)
    # Load task from DB if ID provided
    task_obj = None
    if isinstance(task_identifier, (int, str)):
        if get_task_by_id:
            try:
                task_obj = get_task_by_id(task_identifier)
            except Exception as e:
                logging.warning("Could not fetch task from DB: %s", e)
                task_obj = {'id': task_identifier}
        else:
            task_obj = {'id': task_identifier}
    elif isinstance(task_identifier, dict):
        task_obj = task_identifier
    else:
        task_obj = task_identifier

    _update_db_status(task_obj, 'processing')
    _task_log(task_obj, f"Accepted task {task_identifier}")
    _task_log(task_obj, 'status=processing')

    try:
        brief = getattr(task_obj, 'brief', task_obj.get('brief') if isinstance(task_obj, dict) else None)
        if not brief:
            raise ValueError('Task brief missing')

        # 1. Generate app using LLM with isolated retries
        logging.info('Stage 1: LLM generate')
        _task_log(task_obj, 'stage=generate:start')
        generated = _stage_generate(brief, max_attempts=3)
        _update_db_status(task_obj, 'generated', {'meta': 'llm_ok'})
        _task_log(task_obj, 'stage=generate:ok')

        # Process attachments (download and merge into app_code)
        generated = _process_attachments(task_obj, generated)

        # 2. Push to GitHub (repo creation & pages) - independent retries
        logging.info('Stage 2: Repo push')
        _task_log(task_obj, 'stage=repo_push:start')
        repo_hint = getattr(task_obj, 'task', None) or getattr(task_obj, 'id', None) or 'tds-task'
        repo_url, commit_sha, pages_url = _stage_repo_push(generated, repo_hint)
        _update_db_status(task_obj, 'pushed', {'repo_url': repo_url, 'commit_sha': commit_sha, 'pages_url': pages_url})
        _task_log(task_obj, f'stage=repo_push:ok repo={repo_url} commit={commit_sha} pages={pages_url}')

        # 3. Notify evaluation URL
        logging.info('Stage 3: Notify evaluator')
        _task_log(task_obj, 'stage=notify:start')
        evaluation_url = getattr(task_obj, 'evaluation_url', task_obj.get('evaluation_url') if isinstance(task_obj, dict) else None)
        if not evaluation_url:
            logging.warning('No evaluation_url provided; skipping notify stage')
            _task_log(task_obj, 'stage=notify:skipped')
        else:
            notified = _stage_notify_eval(task_obj, repo_url, commit_sha, pages_url, evaluation_url)
            if notified:
                _update_db_status(task_obj, 'notified', {'notified_to': evaluation_url})
            else:
                # Mark notify failed but continue; do not raise
                _update_db_status(task_obj, 'notify_failed', {'notified_to': evaluation_url})
                _task_log(task_obj, 'stage=notify:failed')

        _update_db_status(task_obj, 'done', {'repo_url': repo_url, 'pages_url': pages_url})
        _task_log(task_obj, 'status=done')
        logging.info('Task %s completed successfully', getattr(task_obj, 'id', None))

    except Exception as e:
        # Log exception and update task state, but do not re-raise to avoid crashing
        # the ASGI background worker thread. The task will be marked as failed
        # and the server can continue handling requests.
        logging.exception('Task processing failed: %s', e)
        _update_db_status(task_obj, 'failed', {'error': str(e)})
        _task_log(task_obj, f'status=failed error={e}')
        # Do not re-raise to keep background thread stable. Caller expects fire-and-forget.
        return

if __name__ == '__main__':
    # Quick smoke test (non-network)
    fake_task = {'id': 'local-test-1', 'brief': 'Create a single page app that shows Hello World', 'email': 'test@example.com', 'task': 'local-test', 'round': 1, 'nonce': 'abc'}
    try:
        process_task(fake_task)
    except Exception as e:
        print('Process task test failed (expected if GitHub utilities not implemented):', e)
