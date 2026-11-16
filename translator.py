import time

from deep_translator import GoogleTranslator

PAUSE = 0.6          # seconds between requests (Google allows ~5/sec, we stay well under)
RETRIES = 5


def _safe_translate(tr, text):
    """Translate with retry + exponential backoff when Google says 'too many requests'."""
    delay = 3
    for attempt in range(RETRIES):
        try:
            result = tr.translate(text)
            time.sleep(PAUSE)
            return result or ""
        except Exception as e:
            if attempt == RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2


def translate_lines(lines, target, on_progress=lambda done, total: None):
    """Translate in a few big batches; only falls back to slow line-by-line if needed."""
    tr = GoogleTranslator(source="auto", target=target)
    out, batch, size = [], [], 0
    total = len(lines)
    done = 0

    def flush():
        nonlocal batch, size, done
        if not batch:
            return
        joined = "\n".join(b["text"] for b in batch)
        translated = _safe_translate(tr, joined)
        pieces = translated.split("\n")
        if len(pieces) != len(batch):
            # Line count changed: translate line by line, slowly (PAUSE between each)
            pieces = []
            for b in batch:
                pieces.append(_safe_translate(tr, b["text"]) or b["text"])
                on_progress(done + len(pieces), total)
        for b, p in zip(batch, pieces):
            out.append({"time": b["time"], "text": p})
        done += len(batch)
        on_progress(done, total)
        batch, size = [], 0

    for line in lines:
        if size + len(line["text"]) > 3000:
            flush()
        batch.append(line)
        size += len(line["text"]) + 1
    flush()
    return out
