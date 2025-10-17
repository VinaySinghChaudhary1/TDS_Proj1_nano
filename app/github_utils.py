"""
GitHub helper utilities for TDS LLM Deployer
- Create or check repositories
- Push files (text / binary)
- Enable GitHub Pages (static, legacy or Actions)
- Generate MIT license
"""

import base64
import json
import logging
import os
import time
from typing import Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .settings import settings

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"


def _get_session_with_retries():
    """Create a requests session with retry strategy for GitHub API calls."""
    session = requests.Session()
    
    # Define retry strategy
    retry_strategy = Retry(
        total=3,  # Total number of retries
        backoff_factor=1,  # Wait time between retries
        status_forcelist=[429, 500, 502, 503, 504],  # HTTP status codes to retry
        allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE"]
    )
    
    # Mount adapter with retry strategy
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session


# ---------------------------------------------------------------------
# Headers helper
# ---------------------------------------------------------------------
def _headers(token: Optional[str] = None):
    token = token or settings.GITHUB_TOKEN
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "tds-llm-deployer",
    }


# ---------------------------------------------------------------------
# Repo creation
# ---------------------------------------------------------------------
def repo_exists(owner: str, repo: str) -> bool:
    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_headers(), timeout=10)
            return r.status_code == 200
        except requests.exceptions.RequestException as e:
            logger.warning("repo_exists connection error (attempt %s): %s", attempt + 1, e)
            time.sleep(2)
    raise ConnectionError(f"Failed to reach GitHub after 3 attempts: {url}")



def create_repo(repo_name: str, description: str = "", private: bool = False) -> dict:
    """
    Create a repo for the authenticated user.
    Uses auto_init=True so that a default branch exists immediately.
    """
    owner = settings.GITHUB_OWNER

    if repo_exists(owner, repo_name):
        logger.info("Repo %s/%s already exists", owner, repo_name)
        r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo_name}", headers=_headers())
        r.raise_for_status()
        return r.json()

    payload = {
        "name": repo_name,
        "description": description or "TDS auto-generated repo",
        "private": private,
        "auto_init": True,           # ensures a main branch is created
        "license_template": "mit",   # adds initial commit
    }

    url = f"{GITHUB_API}/user/repos"
    r = requests.post(url, headers=_headers(), json=payload)
    if r.status_code not in (200, 201):
        logger.error("Failed to create repo: %s %s", r.status_code, r.text)
        r.raise_for_status()
    logger.info("Created repo %s/%s", owner, repo_name)
    return r.json()


def set_default_branch(owner: str, repo: str, branch: str = "main"):
    """Ensure the repo default branch is 'main'."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    payload = {"default_branch": branch}
    r = requests.patch(url, headers=_headers(), json=payload)
    if r.status_code not in (200, 201):
        logger.warning("Failed to set default branch: %s %s", r.status_code, r.text)
    else:
        logger.info("Default branch set to %s for %s/%s", branch, owner, repo)


# ---------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------
def _get_file_sha(owner: str, repo: str, path: str, branch: str = "main") -> Optional[str]:
    """Get SHA of a file with retry logic for network issues."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    session = _get_session_with_retries()
    
    try:
        r = session.get(url, headers=_headers(), params={"ref": branch}, timeout=30)
        if r.status_code == 200:
            return r.json().get("sha")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning("Network error getting file SHA for %s: %s", path, e)
        # Try one more time with a simple requests call
        try:
            time.sleep(2)
            r = requests.get(url, headers=_headers(), params={"ref": branch}, timeout=45)
            if r.status_code == 200:
                return r.json().get("sha")
            if r.status_code == 404:
                return None
        except Exception as e2:
            logger.error("Failed to get file SHA after retry for %s: %s", path, e2)
            return None
    except Exception as e:
        logger.error("Unexpected error getting file SHA for %s: %s", path, e)
        return None


def create_or_update_file(
    owner: str,
    repo: str,
    path: str,
    content_text: str,
    commit_message: str,
    branch: str = "main",
    committer: Optional[dict] = None,
) -> dict:
    """Create or update a text file on GitHub with retry logic."""
    sha = _get_file_sha(owner, repo, path, branch=branch)
    content_b64 = base64.b64encode(content_text.encode("utf-8")).decode("utf-8")

    payload = {"message": commit_message, "content": content_b64, "branch": branch}
    if committer:
        payload["committer"] = committer
    if sha:
        payload["sha"] = sha

    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    session = _get_session_with_retries()
    
    try:
        r = session.put(url, headers=_headers(), json=payload, timeout=30)
        if r.status_code not in (200, 201):
            logger.error("Failed to push %s: %s %s", path, r.status_code, r.text)
            r.raise_for_status()
        return r.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning("Network error pushing file %s: %s", path, e)
        # Try one more time with simple requests
        try:
            time.sleep(2)
            r = requests.put(url, headers=_headers(), json=payload, timeout=45)
            if r.status_code not in (200, 201):
                logger.error("Failed to push %s after retry: %s %s", path, r.status_code, r.text)
                r.raise_for_status()
            return r.json()
        except Exception as e2:
            logger.error("Failed to push %s after retry: %s", path, e2)
            raise
    except Exception as e:
        logger.error("Unexpected error pushing %s: %s", path, e)
        raise


def create_or_update_binary_file(
    owner: str,
    repo: str,
    path: str,
    binary_bytes: bytes,
    commit_message: str,
    branch: str = "main",
) -> dict:
    """Push a binary file (base64 encoded) with retry logic."""
    sha = _get_file_sha(owner, repo, path, branch=branch)
    content_b64 = base64.b64encode(binary_bytes).decode("utf-8")
    payload = {"message": commit_message, "content": content_b64, "branch": branch}
    if sha:
        payload["sha"] = sha

    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    session = _get_session_with_retries()
    
    try:
        r = session.put(url, headers=_headers(), json=payload, timeout=30)
        if r.status_code not in (200, 201):
            logger.error("Binary push failed %s: %s %s", path, r.status_code, r.text)
            r.raise_for_status()
        return r.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        logger.warning("Network error pushing binary file %s: %s", path, e)
        # Try one more time
        try:
            time.sleep(2)
            r = requests.put(url, headers=_headers(), json=payload, timeout=45)
            if r.status_code not in (200, 201):
                logger.error("Binary push failed %s after retry: %s %s", path, r.status_code, r.text)
                r.raise_for_status()
            return r.json()
        except Exception as e2:
            logger.error("Failed to push binary file %s after retry: %s", path, e2)
            raise


# ---------------------------------------------------------------------
# Wait helper before enabling Pages
# ---------------------------------------------------------------------
def wait_for_index(owner: str, repo: str, timeout: int = 30) -> bool:
    """Poll until index.html exists in repo contents."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/index.html"
    start = time.time()
    while True:
        r = requests.get(url, headers=_headers())
        if r.status_code == 200:
            logger.info("index.html detected for %s/%s", owner, repo)
            return True
        if time.time() - start > timeout:
            logger.warning("index.html not found within %ss", timeout)
            return False
        time.sleep(2)


# ---------------------------------------------------------------------
# GitHub Pages enablement (robust)
# ---------------------------------------------------------------------
def enable_pages_and_wait(
    owner: str,
    repo: str,
    branch: str = "main",
    path: str = "/",
    timeout: int = 180,
    poll_interval: float = 2.0,
) -> Tuple[str, dict]:
    """Enable GitHub Pages and wait until site is live."""
    wait_for_index(owner, repo)  # ensure files are there
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pages"
    payload = {"build_type": "legacy", "source": {"branch": branch, "path": path}}
    logger.info("Enabling Pages for %s/%s (branch=%s)", owner, repo, branch)

    r = requests.post(url, headers=_headers(), json=payload)
    if r.status_code not in (200, 201, 202, 204):
        r2 = requests.put(url, headers=_headers(), json=payload)
        if r2.status_code not in (200, 201, 202, 204):
            logger.error("Failed enabling Pages %s/%s (%s): %s", owner, repo, r2.status_code, r2.text)
            r2.raise_for_status()
        response = r2
    else:
        response = r

    # handle empty body
    if not response.text.strip():
        pages_resp = {"status": "no_content"}
    else:
        try:
            pages_resp = response.json()
        except Exception:
            pages_resp = {"status": f"non_json_{response.status_code}", "raw": response.text}

    pages_url = pages_resp.get("html_url") or f"https://{owner}.github.io/{repo}/"
    logger.info("Pages requested; polling until live: %s", pages_url)

    # Poll for live site
    start = time.time()
    while True:
        try:
            ping = requests.get(pages_url, timeout=8)
            if ping.status_code == 200:
                logger.info("✅ GitHub Pages live: %s", pages_url)
                return pages_url, pages_resp
        except requests.RequestException:
            pass

        if time.time() - start > timeout:
            logger.warning("Pages not live within %ss", timeout)
            return pages_url, pages_resp
        time.sleep(poll_interval)

# ---------------------------------------------------------------------
# Optional: Add Actions workflow for Pages
# ---------------------------------------------------------------------
def add_pages_workflow(owner: str, repo: str):
    """Add GitHub Actions workflow file for Pages as fallback."""
    yaml_content = """name: Deploy to GitHub Pages
on:
  push:
    branches:
      - main
permissions:
  contents: read
  pages: write
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Upload artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: '.'
      - name: Deploy to Pages
        id: deploy
        uses: actions/deploy-pages@v4
"""
    create_or_update_file(owner, repo, ".github/workflows/pages.yml", yaml_content, "add pages.yml")


# ---------------------------------------------------------------------
# High-level helper for worker
# ---------------------------------------------------------------------
def create_repo_and_push(generated: dict, repo_hint: str) -> Tuple[str, str, str]:
    """
    Combined utility used by worker.py.
    Creates repo, pushes app_code, readme, license, and enables Pages.
    """
    owner = settings.GITHUB_OWNER
    repo = f"{repo_hint}".replace(" ", "-").lower()
    logger.info("Creating and pushing repo %s/%s", owner, repo)

    repo_info = create_repo(repo, description="TDS auto-deployed app")
    # app_code may be a string (monolithic index.html) or a mapping of filename -> content
    app_code = generated.get("app_code")
    if isinstance(app_code, dict):
        # push each filename individually
        for fname, content in app_code.items():
            if isinstance(content, str):
                logger.info("Pushing file %s to repo %s/%s", fname, owner, repo)
                create_or_update_file(owner, repo, fname, content, f"add {fname}")
            elif isinstance(content, (bytes, bytearray)):
                logger.info("Pushing binary file %s to repo %s/%s", fname, owner, repo)
                create_or_update_binary_file(owner, repo, fname, bytes(content), f"add {fname}")
            else:
                # Fallback: convert to string
                logger.info("Pushing file %s (converted to text) to repo %s/%s", fname, owner, repo)
                create_or_update_file(owner, repo, fname, str(content), f"add {fname}")
        # ensure index.html exists or set pages to first HTML file
        if "index.html" not in app_code:
            # no explicit index; nothing to do here, Pages enable will look for index.html
            logger.debug("No index.html present in generated app_code; proceeding without explicit index.html")
    else:
        # assume app_code is a single string to be written as index.html
        create_or_update_file(owner, repo, "index.html", str(app_code or ""), "add app_code")

    create_or_update_file(owner, repo, "README.md", generated.get("readme", ""), "add readme")
    create_or_update_file(owner, repo, "LICENSE", generated.get("license", ""), "add license")

    pages_url, _ = enable_pages_and_wait(owner, repo)
    commit_sha = repo_info.get("pushed_at", str(time.time()))
    return f"https://github.com/{owner}/{repo}", commit_sha, pages_url



# ---------------------------------------------------------------------
# License
# ---------------------------------------------------------------------
def generate_mit_license(author: Optional[str] = None, year: Optional[str] = None) -> str:
    year_text = year or time.strftime("%Y")
    author_text = author or settings.GITHUB_OWNER or "Author"
    return f"""MIT License

Copyright (c) {year_text} {author_text}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
