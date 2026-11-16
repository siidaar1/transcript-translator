"""Translate transcript lines by driving the Gemini website with Selenium (no API, no login needed).

Two ways it gets a Chrome window:
1) If a Chrome started with --remote-debugging-port=9222 is already open (start_chrome scripts), it attaches to it.
2) Otherwise it opens its own Chrome window and uses Gemini as a guest (no sign-in).
"""
import atexit
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

DEBUG_ADDR = "127.0.0.1:9222"
GEMINI_URL = "https://gemini.google.com/app"
HEADLESS = True        # True = hidden Chrome. Set False to watch the window.
SHOT_PATH = Path(__file__).resolve().parent / "gemini_error.png"
CHUNK_CHARS = 4000          # max characters sent to Gemini per message
ANSWER_TIMEOUT = 240        # seconds to wait for one answer

INPUT_SELECTORS = [
    "rich-textarea div.ql-editor[contenteditable='true']",
    "div.ql-editor[contenteditable='true']",
    "div[contenteditable='true'][role='textbox']",
]
SEND_SELECTORS = [
    "button.send-button",
    "button[aria-label='Send message']",
    "button[aria-label*='Send']",
]
STOP_SELECTORS = ["button[aria-label*='Stop']", "button.stop"]
RESPONSE_SELECTORS = ["message-content", ".model-response-text", "model-response"]

POPUP_BUTTONS = ["stay signed out", "no thanks", "not now", "got it", "dismiss", "close",
                 "continue without signing in", "accept all", "i agree", "reject all"]

_driver = None
_tab = None
_own = False
_profile = None      # temporary Chrome profile folder (deleted when the app stops)


def _debug_port_open():
    try:
        with socket.create_connection(("127.0.0.1", 9222), timeout=0.7):
            return True
    except OSError:
        return False


def _chrome_options(profile):
    opts = Options()
    if profile is not None:
        opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    if HEADLESS:
        opts.add_argument("--headless=new")
    return opts


def _launch_own_chrome():
    """Chrome of its own (hidden when HEADLESS is True). Gemini guest mode needs no login.
    Uses a fresh temporary profile, so a leftover Chrome can never block it."""
    global _profile
    last = None
    for fresh_profile in (True, False):     # 2nd try: let Chrome pick its own default profile
        profile = Path(tempfile.mkdtemp(prefix="gemini_chrome_")) if fresh_profile else None
        try:
            driver = webdriver.Chrome(options=_chrome_options(profile))
        except WebDriverException as e:
            last = e
            if profile is not None:
                shutil.rmtree(profile, ignore_errors=True)
            continue
        _profile = profile
        if HEADLESS:
            try:   # a hidden Chrome calls itself "HeadlessChrome": show the normal name instead
                ua = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
                driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
            except WebDriverException:
                pass
        return driver
    raise last


def _cleanup():
    """Runs when the app stops: close our own Chrome (never a Chrome you started yourself)."""
    if _own and _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
    if _profile is not None:
        shutil.rmtree(_profile, ignore_errors=True)


atexit.register(_cleanup)


def _hint(driver):
    """Extra help for error messages when you cannot see the window."""
    if not (_own and HEADLESS):
        return ""
    try:
        driver.save_screenshot(str(SHOT_PATH))
        shot = f" A screenshot was saved as {SHOT_PATH.name}."
    except WebDriverException:
        shot = ""
    return shot + " To watch it, set HEADLESS = False at the top of gemini_bot.py."


def _get_driver():
    global _driver, _tab, _own
    if _driver is not None:
        try:
            _driver.window_handles
        except WebDriverException:
            _driver, _tab = None, None
    if _driver is None:
        try:
            if _debug_port_open():                      # a logged-in Chrome is already open: use it
                opts = Options()
                opts.add_experimental_option("debuggerAddress", DEBUG_ADDR)
                _driver, _own = webdriver.Chrome(options=opts), False
            else:                                       # default: own Chrome, guest mode
                _driver, _own = _launch_own_chrome(), True
        except WebDriverException as e:
            first = (str(e).strip().splitlines() or ["unknown error"])[0]
            raise RuntimeError(
                "Could not open Chrome for Gemini: " + first +
                " - close leftover Chrome windows opened by the bot (Task Manager, chrome.exe) and try again, "
                "or set HEADLESS = False at the top of gemini_bot.py."
            )
    if _tab is None or _tab not in _driver.window_handles:
        if _own:
            _tab = _driver.window_handles[0]
        else:
            _driver.switch_to.new_window("tab")
            _tab = _driver.current_window_handle
    _driver.switch_to.window(_tab)
    return _driver


def _first(driver, selectors, timeout=0):
    end = time.time() + timeout
    while True:
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return els[-1]
        if time.time() >= end:
            return None
        time.sleep(0.5)


def _dismiss_popups(driver):
    """Click away sign-in suggestions, cookie and welcome dialogs."""
    try:
        return driver.execute_script("""
            const want = arguments[0];
            for (const b of document.querySelectorAll('button, a[role=button], div[role=button]')) {
              const t = (b.innerText || b.getAttribute('aria-label') || '').trim().toLowerCase();
              if (t && want.includes(t)) { b.click(); return t; }
            }
            return null;""", POPUP_BUTTONS)
    except WebDriverException:
        return None


def _sign_in_needed():
    return RuntimeError(
        "Gemini asks you to sign in, so guest mode is not available here. "
        "Use another engine, or log in once with the start_chrome script."
    )


def _open_new_chat(driver):
    driver.get(GEMINI_URL)
    end = time.time() + 45
    while time.time() < end:
        if "accounts.google.com" in driver.current_url:
            raise _sign_in_needed()
        box = _first(driver, INPUT_SELECTORS)
        if box is not None and box.is_displayed():
            _dismiss_popups(driver)
            time.sleep(0.5)
            return _first(driver, INPUT_SELECTORS, timeout=5) or box
        _dismiss_popups(driver)
        time.sleep(1)
    raise RuntimeError(
        "Could not find the Gemini message box (a popup or a sign-in wall may be in the way)." + _hint(driver)
    )


def _type_prompt(driver, box, text):
    driver.execute_script(
        "const el=arguments[0];el.focus();"
        "document.execCommand('selectAll',false,null);"
        "document.execCommand('insertText',false,arguments[1]);",
        box, text,
    )
    time.sleep(0.8)
    typed = (box.get_attribute("innerText") or "").strip()
    if len(typed) < min(20, len(text)):
        # fallback: type line by line, Shift+Enter makes a new line (Enter would send)
        box.click()
        for i, line in enumerate(text.split("\n")):
            if i:
                box.send_keys(Keys.SHIFT, Keys.ENTER)
            box.send_keys(line)


def _send(driver, box):
    time.sleep(0.5)
    btn = _first(driver, SEND_SELECTORS)
    if btn is not None:
        driver.execute_script("arguments[0].click();", btn)
    else:
        box.send_keys(Keys.ENTER)


def _last_answer(driver):
    for sel in RESPONSE_SELECTORS:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            return (els[-1].get_attribute("innerText") or "").strip()
    return ""


def _wait_answer(driver, on_status):
    if _first(driver, RESPONSE_SELECTORS, timeout=60) is None:
        if "accounts.google.com" in driver.current_url:
            raise _sign_in_needed()
        raise RuntimeError(
            "Gemini did not answer (it may ask you to sign in, or a guest limit was reached)." + _hint(driver)
        )
    on_status("Gemini is writing the translation")
    last, changed_at = "", time.time()
    end = time.time() + ANSWER_TIMEOUT
    while time.time() < end:
        time.sleep(2)
        text = _last_answer(driver)
        generating = driver.find_elements(By.CSS_SELECTOR, ",".join(STOP_SELECTORS))
        if text != last:
            last, changed_at = text, time.time()
        elif text and not generating and time.time() - changed_at >= 4:
            return text
    if last:
        return last
    raise RuntimeError("Timed out waiting for Gemini's answer.")


def _chunks(lines):
    cur, size = [], 0
    for ln in lines:
        n = len(ln["text"]) + 12
        if cur and size + n > CHUNK_CHARS:
            yield cur
            cur, size = [], 0
        cur.append(ln)
        size += n
    if cur:
        yield cur


_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_TIMED = re.compile(r"^\s*[\[\(]?\s*(\d+:\d{2}(?::\d{2})?)\s*[\]\)]?\s*[-:]?\s*(.*)$")
_UNTIMED = re.compile(r"^\s*\[\?\]\s*(.*)$")


def _parse_lines(answer, chunk):
    """Turn Gemini's answer back into [{'time','text'}, ...]."""
    out = []
    for raw in answer.splitlines():
        line = raw.translate(_DIGITS).strip()      # Gemini may write times with Arabic digits
        if not line or set(line) <= {"`"}:
            continue
        m = _TIMED.match(line)
        if m:
            out.append({"time": m.group(1), "text": m.group(2).strip()})
            continue
        m = _UNTIMED.match(line)
        if m:
            out.append({"time": "", "text": m.group(1).strip()})
        elif out:                                   # a wrapped line: join with the previous one
            out[-1]["text"] += " " + raw.strip()
        else:
            out.append({"time": "", "text": raw.strip()})
    if len(out) == len(chunk):                      # same line count: use the original times
        for o, c in zip(out, chunk):
            o["time"] = c["time"]
    return out or chunk


_MARK = re.compile(r"@@\s*([0-9٠-٩۰-۹]+)\s*@@")


def _parse(answer, chunk):
    """Split Gemini's answer by the @@n@@ markers: line n of the answer = line n of the original."""
    pieces = _MARK.split(answer)          # ['', '1', 'text', '2', 'text', ...]
    found = {}
    for i in range(1, len(pieces) - 1, 2):
        found[int(pieces[i].translate(_DIGITS))] = " ".join(pieces[i + 1].split())
    if not found:                         # Gemini ignored the markers
        return _parse_lines(answer, chunk)
    return [{"time": c["time"], "text": found.get(n, "")} for n, c in enumerate(chunk, 1)]


def _build_prompt(language, chunk, details=""):
    n = len(chunk)
    body = "\n".join(f"@@{i}@@ {l['text']}" for i, l in enumerate(chunk, 1))
    return (
        f"You are a professional subtitle translator. Translate the {n} numbered subtitle lines below into {language}.\n"
        + (f"About the language: {details}.\n" if details else "") + "\n"
        "Meaning rules (the most important part):\n"
        f"- Translate the MEANING, not the letters and not word by word. Write natural, fluent {language}, "
        "the way a native speaker would say it in a video.\n"
        f"- Do NOT transliterate. Never write an English word by spelling its sound with {language} letters. "
        f"Use the real {language} word for every ordinary word. Only brand names, product names and people's names "
        "may stay as they are.\n"
        "- The lines are pieces of longer sentences from automatic captions. Read the neighbouring lines to understand "
        "the whole sentence, then translate each line so it makes sense in its own place. "
        "If a number or word is clearly broken by the captions, use the meaning from the context.\n\n"
        "Format rules:\n"
        f"- Each line starts with a marker like @@1@@. Output exactly {n} lines, each starting with the SAME marker, "
        "followed by the translation of ONLY that line's text.\n"
        "- Keep the lines separate. Never move words between lines, never merge or skip a line.\n"
        "- Keep each line short and natural, like subtitles.\n"
        f"- Reply with only the {n} marker lines: no introduction, no notes, no code block.\n\n" + body
    )


def gemini_translate(lines, language, on_status=lambda msg: None, details=""):
    driver = _get_driver()
    parts = list(_chunks(lines))
    result = []
    for i, chunk in enumerate(parts, 1):
        tag = f"part {i} of {len(parts)}"
        on_status(f"Opening Gemini ({tag})")
        box = _open_new_chat(driver)

        prompt = _build_prompt(language, chunk, details)
        on_status(f"Sending the transcript to Gemini ({tag})")
        _type_prompt(driver, box, prompt)
        _send(driver, box)
        answer = _wait_answer(driver, lambda m: on_status(f"{m} ({tag})"))
        result.extend(_parse(answer, chunk))
    return result