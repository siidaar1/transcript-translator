"""Selenium bot: opens a YouTube video, opens the transcript panel, scrapes it."""
import re
import time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


TIME_RE = re.compile(r"^\d+:\d{2}(?::\d{2})?$")
DURATION_RE = re.compile(
    r"^\d+\s+(?:hours?|minutes?|seconds?)(?:,?\s*(?:and\s+)?\d+\s+(?:hours?|minutes?|seconds?))*$",
    re.I,
)


def make_driver(headless=True):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--mute-audio")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    # Selenium 4.6+ downloads/manages chromedriver automatically.
    return webdriver.Chrome(options=opts)


def _click_js(driver, element):
    driver.execute_script("arguments[0].click();", element)


def _dismiss_consent(driver):
    """Click 'Accept all / Reject all' on the cookie dialog if it shows up."""
    for xpath in (
        "//button[.//span[contains(text(),'Accept all')]]",
        "//button[.//span[contains(text(),'Reject all')]]",
        "//button[@aria-label='Accept the use of cookies and other data for the purposes described']",
    ):
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            _click_js(driver, btn)
            time.sleep(1)
            return
        except TimeoutException:
            continue


def extract_transcript(url, on_step=lambda msg: None, headless=True, info=None):
    """Return transcript as a list of {'time': '0:05', 'text': '...'}"""
    driver = make_driver(headless)
    try:
        on_step("Opening the video page")
        driver.get(url)
        wait = WebDriverWait(driver, 25)
        _dismiss_consent(driver)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#description")))

        # pause the video so it does not keep playing in the background
        driver.execute_script("var v=document.querySelector('video'); if(v){v.pause();}")

        on_step("Expanding the description")
        driver.execute_script("window.scrollTo(0, 400);")
        for sel in ("#description-inline-expander #expand", "tp-yt-paper-button#expand", "#expand"):
            try:
                btn = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                _click_js(driver, btn)
                break
            except TimeoutException:
                continue

        on_step("Opening the transcript panel")
        btn = None
        for sel in (
            "ytd-video-description-transcript-section-renderer button",
            "button[aria-label='Show transcript']",
        ):
            try:
                btn = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                break
            except TimeoutException:
                continue
        if btn is None:
            raise RuntimeError("This video has no transcript button (captions may be disabled).")
        _click_js(driver, btn)

        on_step("Reading transcript lines")
        seg_selector = "ytd-transcript-segment-renderer, transcript-segment-view-model"
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, seg_selector)))
        time.sleep(1.5)

        segments = driver.find_elements(By.CSS_SELECTOR, seg_selector)
        out = []
        for seg in segments:
            raw = (seg.get_attribute("innerText") or seg.text or "").strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split("\n") if p.strip()]
            time_label = ""
            if parts and TIME_RE.match(parts[0]):
                time_label = parts.pop(0)
                # drop the hidden accessibility text, e.g. "8 seconds" / "1 minute, 5 seconds"
                while parts and DURATION_RE.match(parts[0]):
                    parts.pop(0)
            text = " ".join(parts).strip()
            if text:
                out.append({"time": time_label, "text": text})
        if not out:
            raise RuntimeError("Transcript panel opened but no lines were found.")
        if info is not None:
            title = re.sub(r"^\(\d+\)\s*", "", driver.title or "")
            info["title"] = re.sub(r"\s*-\s*YouTube$", "", title).strip()
        return out
    finally:
        driver.quit()