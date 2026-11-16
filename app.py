import threading
import uuid

from flask import Flask, jsonify, render_template, request

import github_sync
import storage
from bot import extract_transcript
from gemini_bot import gemini_translate
from translator import translate_lines

app = Flask(__name__)
JOBS = {}
GEMINI_LOCK = threading.Lock()   # one Gemini chat tab -> one job at a time

STEPS = [
    ("open", "Open the video"),
    ("extract", "Extract the transcript"),
    ("translate", "Translate the transcript"),
    ("done", "Show the result"),
]

LANGS = {
    "ar": "Arabic", "ku": "Kurdish (Kurmanji)", "ckb": "Kurdish (Sorani)", "badini": "Kurdish (Badini)",
    "en": "English", "tr": "Turkish", "fa": "Persian", "fr": "French",
    "es": "Spanish", "de": "German",
}

# Extra description sent to Gemini once in the prompt (helps with Kurdish dialects)
LANG_DETAILS = {
    "badini": "Badini (Bahdini) dialect of Kurdish as spoken in Duhok, written in the Kurdish Arabic-script alphabet "
              "(بەهدینی). Use Badini vocabulary and grammar, not Sorani, and not Latin letters",
    "ckb": "Sorani dialect of Kurdish, written in the Kurdish Arabic-script alphabet",
    "ku": "Kurmanji dialect of Kurdish, written in Latin letters (Hawar alphabet)",
}


def run_job(job_id, url, lang, engine):
    job = JOBS[job_id]

    def set_step(key, state, detail=""):
        job["steps"][key] = {"state": state, "detail": detail}

    def save(status):
        """Save the job in the 'saved' folder. Returns a short text for the last step."""
        try:
            job["saved_id"] = storage.save_job(job, LANGS.get(lang, lang), status)
        except Exception as e:
            job["save_error"] = str(e)
            return f"could not save: {e}"
        note = f"saved in saved/{job['saved_id']}"
        if github_sync.enabled():
            set_step("done", "running", "Uploading to GitHub…")
            try:
                github_sync.upload_job(job["saved_id"])
                note += "; uploaded to GitHub"
            except Exception as e:
                job["github_error"] = str(e)
                note += f"; GitHub upload failed ({e})"
        return note

    try:
        set_step("open", "running", "Starting the browser bot")

        def on_step(msg):
            if msg.startswith("Opening the video"):
                set_step("open", "running", msg)
            else:
                set_step("open", "done", "Video page loaded")
                set_step("extract", "running", msg)

        info = {}
        lines = extract_transcript(url, on_step=on_step, info=info)
        job["title"] = info.get("title") or url
        set_step("open", "done", "Video page loaded")
        set_step("extract", "done", f"{len(lines)} lines extracted")
        job["original"] = lines

        set_step("translate", "running", "Starting…")
        try:
            status = lambda m: set_step("translate", "running", m)
            if engine == "gemini":
                set_step("translate", "running", "Waiting for the Gemini tab")
                with GEMINI_LOCK:
                    translated = gemini_translate(lines, LANGS.get(lang, lang), status, LANG_DETAILS.get(lang, ""))
            elif lang == "badini":
                raise RuntimeError("Google Translate does not support Badini. Choose the Gemini engine for this language.")
            else:
                translated = translate_lines(
                    lines, lang, lambda d, t: status(f"Translated {d} of {t} lines"))
        except Exception as e:
            set_step("translate", "error", str(e)[:250])
            note = save("partial")
            set_step("done", "done", "Showing the original transcript (translation failed); " + note)
            job["error"] = "Translation failed: " + str(e)[:250]
            job["status"] = "partial"
            return
        job["translated"] = translated
        set_step("translate", "done", f"{len(translated)} lines translated")
        note = save("done")
        set_step("done", "done", note)
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        for key, _ in STEPS:
            if job["steps"][key]["state"] == "running":
                job["steps"][key] = {"state": "error", "detail": str(e)}


@app.route("/")
def index():
    return render_template("index.html", steps=STEPS)


@app.post("/start")
def start():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    lang = (data.get("lang") or "ar").strip()
    engine = (data.get("engine") or "gemini").strip()
    if "youtube.com" not in url and "youtu.be" not in url:
        return jsonify(error="Please paste a YouTube video link."), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "running",
        "url": url, "lang": lang, "engine": engine, "title": "",
        "steps": {k: {"state": "waiting", "detail": ""} for k, _ in STEPS},
        "original": [],
        "translated": [],
        "error": "",
    }
    threading.Thread(target=run_job, args=(job_id, url, lang, engine), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    return jsonify(job)


@app.get("/history")
def history():
    return jsonify(storage.list_saved())


@app.get("/history/<saved_id>")
def history_item(saved_id):
    item = storage.load_saved(saved_id)
    if item is None:
        return jsonify(error="Not found"), 404
    return jsonify(item)


@app.delete("/history/<saved_id>")
def history_delete(saved_id):
    if not storage.delete_saved(saved_id):
        return jsonify(error="Not found"), 404
    out = {"ok": True}
    if github_sync.enabled():                 # also remove it from GitHub
        try:
            github_sync.delete_job(saved_id)
        except Exception as e:
            out["github_error"] = str(e)
    return jsonify(out)


@app.get("/github/status")
def github_status():
    cfg = github_sync.config()
    return jsonify(enabled=cfg is not None, repo=cfg["repo"] if cfg else "")


@app.post("/github/sync")
def github_sync_all():
    if not github_sync.enabled():
        return jsonify(error="GitHub sync is not set up"), 400
    try:
        uploaded, already = github_sync.sync_all()
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(uploaded=uploaded, already=already)


if __name__ == "__main__":
    app.run(debug=False, port=5000, threaded=True)