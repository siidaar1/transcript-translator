"""Saves every finished job in the 'saved' folder and reads them back."""
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

SAVE_DIR = Path(__file__).resolve().parent / "saved"


def video_id(url):
    m = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/|/embed/|/live/)([\w-]{11})", url or "")
    return m.group(1) if m else ""


def _sec(t):
    parts = [int(x) for x in t.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]


def _stamp(sec):
    return f"{sec // 3600:02}:{sec % 3600 // 60:02}:{sec % 60:02},000"


def to_srt(lines):
    """Subtitle file text. Each line ends where the next one starts."""
    rows = [l for l in lines if l.get("time")]
    out = []
    for i, l in enumerate(rows):
        if not l.get("text"):
            continue
        start = _sec(l["time"])
        end = _sec(rows[i + 1]["time"]) if i + 1 < len(rows) else start + 4
        if end <= start:
            end = start + 2
        out.append(f"{len(out) + 1}\n{_stamp(start)} --> {_stamp(end)}\n{l['text']}\n")
    return "\n".join(out)


def save_job(job, language, status):
    """Creates saved/<date>_<videoid>/ with data.json and .srt files. Returns the folder name."""
    now = datetime.now()
    vid = video_id(job["url"]) or "video"
    name = f"{now:%Y-%m-%d_%H-%M-%S}_{vid}"
    folder = SAVE_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "id": name,
        "url": job["url"],
        "video_id": vid,
        "title": job.get("title") or job["url"],
        "lang": job["lang"],
        "language": language,
        "engine": job.get("engine", ""),
        "created": now.isoformat(timespec="seconds"),
        "status": status,
        "original": job["original"],
        "translated": job["translated"],
    }
    (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "original.srt").write_text(to_srt(job["original"]), encoding="utf-8")
    if job["translated"]:
        (folder / f"translated_{job['lang']}.srt").write_text(to_srt(job["translated"]), encoding="utf-8")
    return name


def summary(d):
    """Short info about one saved video (used by the local list and by GitHub's index.json)."""
    return {
        "id": d["id"], "title": d.get("title", ""), "url": d.get("url", ""),
        "video_id": d.get("video_id", ""), "language": d.get("language", ""),
        "created": d.get("created", ""), "lines": len(d.get("original", [])),
        "translated": bool(d.get("translated")),
    }


def list_saved():
    """Short info for every saved job, newest first."""
    items = []
    if not SAVE_DIR.exists():
        return items
    for folder in sorted(SAVE_DIR.iterdir(), reverse=True):
        try:
            d = json.loads((folder / "data.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append(summary(d))
    return items


def load_saved(saved_id):
    if not re.fullmatch(r"[\w-]+", saved_id or ""):
        return None
    path = SAVE_DIR / saved_id / "data.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_saved(saved_id):
    """Removes one saved folder. Returns True if it existed."""
    if not re.fullmatch(r"[\w-]+", saved_id or ""):
        return False
    folder = SAVE_DIR / saved_id
    if not (folder / "data.json").is_file():
        return False
    shutil.rmtree(folder)
    return True