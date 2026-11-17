"""Uploads every saved job to a GitHub repository with the Contents API (no extra packages).

Setup: copy github_config.example.json to github_config.json and fill it in
(or set the environment variables GITHUB_TOKEN and GITHUB_REPO).
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import storage

API = "https://api.github.com"
CONFIG_PATH = Path(__file__).resolve().parent / "github_config.json"


class GitHubError(RuntimeError):
    def __init__(self, code, message):
        hint = {
            401: "the token is wrong or expired",
            403: "the token may not be allowed to write to this repo, or a rate limit was hit",
            404: "wrong repo name, or the token cannot see this repo",
            422: "GitHub refused the data",
        }.get(code, "")
        super().__init__(f"GitHub {code}: {message}" + (f" ({hint})" if hint else ""))
        self.code = code


def config():
    """Returns the settings, or None when GitHub sync is not set up."""
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except ValueError:
            cfg = {}
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or cfg.get("token") or "").strip()
    repo = (os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPO") or cfg.get("repo") or "").strip()
    if not token or "PASTE" in token or "/" not in repo or "your-" in repo.lower():
        return None
    return {
        "token": token,
        "repo": repo,
        "branch": (os.environ.get("GITHUB_BRANCH") or cfg.get("branch") or "main").strip(),
        "folder": (cfg.get("folder") or "saved").strip("/"),
    }


def enabled():
    return config() is not None


def _call(cfg, method, path, body=None):
    url = f"{API}/repos/{cfg['repo']}/contents/{urllib.parse.quote(path)}"
    if method == "GET":
        url += "?ref=" + urllib.parse.quote(cfg["branch"])
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "transcript-translator",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        try:
            msg = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            msg = ""
        raise GitHubError(e.code, msg)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitHub: {e.reason}")


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _put(cfg, path, raw, message):
    for attempt in range(2):
        existing = _call(cfg, "GET", path)
        body = {"message": message, "content": _b64(raw), "branch": cfg["branch"]}
        if isinstance(existing, dict) and existing.get("sha"):
            body["sha"] = existing["sha"]
        try:
            return _call(cfg, "PUT", path, body)
        except GitHubError as e:
            if e.code in (409, 422) and attempt == 0:
                continue
            raise


def _read_index(cfg):
    r = _call(cfg, "GET", f"{cfg['folder']}/index.json")
    if not isinstance(r, dict):
        return [], None
    if r.get("size", 0) > 0 and not r.get("content"):
        raise RuntimeError("index.json is too large for the Contents API")
    try:
        items = json.loads(base64.b64decode(r.get("content", "")).decode("utf-8") or "[]")
    except ValueError:
        items = []
    return items, r.get("sha")


def _update_index(cfg, change, message):
    """Reads saved/index.json, applies change(list) -> list, writes it back."""
    path = f"{cfg['folder']}/index.json"
    for attempt in range(3):
        items, sha = _read_index(cfg)
        items = change(items)
        items.sort(key=lambda e: e.get("created", ""), reverse=True)
        body = {
            "message": message,
            "content": _b64(json.dumps(items, ensure_ascii=False, indent=1).encode("utf-8")),
            "branch": cfg["branch"],
        }
        if sha:
            body["sha"] = sha
        try:
            _call(cfg, "PUT", path, body)
            return items
        except GitHubError as e:
            if e.code in (409, 422) and attempt < 2:
                continue
            raise


def _valid(saved_id):
    return bool(re.fullmatch(r"[\w-]+", saved_id or ""))


def upload_job(saved_id):
    """Uploads saved/<id>/* and adds the video to saved/index.json. Returns the repo name."""
    cfg = config()
    if cfg is None or not _valid(saved_id):
        return None
    folder = storage.SAVE_DIR / saved_id
    data = json.loads((folder / "data.json").read_text(encoding="utf-8"))
    for f in sorted(folder.iterdir()):          # one file after another (GitHub needs them serial)
        if f.is_file():
            _put(cfg, f"{cfg['folder']}/{saved_id}/{f.name}", f.read_bytes(), f"Add {saved_id}: {f.name}")
    summary = storage.summary(data)
    _update_index(cfg, lambda items: [e for e in items if e.get("id") != saved_id] + [summary],
                  f"Update index: add {saved_id}")
    return cfg["repo"]


def delete_job(saved_id):
    """Removes saved/<id>/* and the video from saved/index.json."""
    cfg = config()
    if cfg is None or not _valid(saved_id):
        return None
    listing = _call(cfg, "GET", f"{cfg['folder']}/{saved_id}")
    if isinstance(listing, list):
        for f in listing:
            if f.get("type") == "file":
                _call(cfg, "DELETE", f["path"],
                      {"message": f"Delete {saved_id}: {f['name']}", "sha": f["sha"], "branch": cfg["branch"]})
    _update_index(cfg, lambda items: [e for e in items if e.get("id") != saved_id],
                  f"Update index: remove {saved_id}")
    return cfg["repo"]


def sync_all():
    """Uploads every local saved video that is not on GitHub yet. Returns (uploaded, already_there)."""
    cfg = config()
    if cfg is None:
        return 0, 0
    remote = {e.get("id") for e in _read_index(cfg)[0]}
    uploaded = 0
    for item in storage.list_saved():
        if item["id"] not in remote:
            upload_job(item["id"])
            uploaded += 1
    return uploaded, len(remote)