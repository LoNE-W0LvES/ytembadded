"""The link feeder, run by the app itself: a hidden Chromium (Playwright) logged in to Udvash with
the account saved on the main page. No Chrome, extension or open tab needed on any PC.

When the app needs something from Udvash (a course load, fresh PDF or video links), this opens the
course in the hidden browser, logs in first if the session has run out, and starts the same feeder
script the Chrome extension used (feeder-extension/feeder.js) in that page; the script reads the
cards with the logged-in session and sends what it finds to the app. Once nothing has been needed
for a while the browser closes again.

The script talks to the app through a made-up address on Udvash's own site (RELAY): the browser
hands those requests to this module, which passes them to the app, so they never leave the PC.
(Chromium refuses a website's requests to 127.0.0.1 unless someone clicks "allow", and a hidden
browser has nobody to click.)

All Playwright calls happen on the one thread started by start(); Playwright's sync API is tied to
the thread that started it.
"""
import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

log = logging.getLogger(__name__)

LOGIN_FILE = settings.BASE_DIR / "udvash_login.json"  # {"identifier", "password"}; never sent back out
PROFILE_DIR = settings.BASE_DIR / "udvash_browser"  # the hidden browser's cookies: the session survives restarts
FEEDER_JS = settings.BASE_DIR.parent / "feeder-extension" / "feeder.js"
LOGIN_PATH = "/Account/Login"
IDLE_CLOSE = 10 * 60  # close the browser once nothing has needed Udvash for this long
REVIVE_AFTER = 90  # restart the feeder page when it hasn't checked in for this long

_lock = threading.Lock()
state = {"status": "off", "error": "", "since": 0.0}
_wake = threading.Event()
_forget = threading.Event()
_test = threading.Event()  # log in now (new account saved), even if nothing needs Udvash yet
_rejected = [False]  # Udvash refused the saved login: no retrying (it could lock the account) until it changes
APP = ["http://127.0.0.1:8000"]  # this app, for the relay; set by start()
RELAY = "https://online.udvash-unmesh.com/__coursehub"


def _set(status, error=""):
    with _lock:
        state.update(status=status, error=error, since=time.time())


def account():
    try:
        return json.loads(LOGIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def configured():
    a = account()
    return bool(a.get("identifier") and a.get("password"))


def save_account(identifier, password):
    identifier, password = identifier.strip(), password
    if not identifier or not password:
        raise ValueError("Enter both your registration number (or mobile number) and password.")
    LOGIN_FILE.write_text(json.dumps({"identifier": identifier, "password": password}), encoding="utf-8")
    _forget.set()  # drop the old session: the next login uses this account
    _rejected[0] = False
    _test.set()
    _wake.set()


def forget_account():
    LOGIN_FILE.unlink(missing_ok=True)
    _forget.set()
    _wake.set()


def masked():
    ident = account().get("identifier", "")
    return ("•" * max(len(ident) - 3, 0) + ident[-3:]) if ident else ""


def status():
    with _lock:
        s = dict(state)
    return {"configured": configured(), "user": masked(), **s}


# Database work goes through here: Django won't run it on the browser thread, where Playwright keeps
# an event loop running.
_db = ThreadPoolExecutor(1, thread_name_prefix="udvash-db")


def needed():
    """Does anything need Udvash right now? (a course load, PDFs or videos waiting for links)"""
    return _db.submit(_needed).result()


def _needed():
    from . import course, downloader, to_device
    from .models import Lecture
    return (course.wanted() or bool(downloader.downloads_snapshot()["needs_link"]) or to_device.waiting_for_links()
            or any(not downloader._video_link_ok(lec)
                   for lec in Lecture.objects.filter(status="queued", youtube_id="").exclude(portal_url="")))


def wake():
    """Something new needs Udvash: check now instead of at the next tick."""
    _wake.set()


def start(port):
    APP[0] = f"http://127.0.0.1:{port}"
    threading.Thread(target=_run, name="udvash-browser", daemon=True).start()


class _Browser:
    def __init__(self):
        self.pw = self.ctx = self.page = None
        self.opened = 0.0

    def running(self):
        return self.ctx is not None

    def launch(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        # bypass_csp: Udvash's page rules would stop the feeder script from calling this app.
        self.ctx = self.pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True, bypass_csp=True)
        self.ctx.set_default_timeout(45000)
        self.ctx.route(RELAY + "/**", _relay)

    def close(self):
        for step in (lambda: self.ctx and self.ctx.close(), lambda: self.pw and self.pw.stop()):
            try:
                step()
            except Exception:
                pass
        self.pw = self.ctx = self.page = None

    def open_feeder(self, start_url):
        """Open the course page (logging in if needed) and start the feeder script in it."""
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
        self.page = page = self.ctx.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        if LOGIN_PATH.lower() in page.url.lower():
            self.login(page)
            page.goto(start_url, wait_until="domcontentloaded")
        if LOGIN_PATH.lower() in page.url.lower():
            raise RuntimeError("Udvash still shows its login page after logging in")
        page.on("pageerror", lambda e: log.warning("Udvash browser page: %s", str(e)[:300]))
        # The feeder reads the app's address from this mark, as it did in the extension's tab.
        page.evaluate("app => { history.replaceState(null, '', location.pathname + location.search + '#coursehub-feeder@' + app); }", RELAY)
        page.evaluate("() => {\n" + FEEDER_JS.read_text(encoding="utf-8") + "\n}")
        self.opened = time.time()

    def login(self, page):
        a = account()
        if not (a.get("identifier") and a.get("password")):
            raise RuntimeError("no Udvash account saved: enter it on the main page")
        _set("logging in")
        page.fill("#Identifier", a["identifier"])
        page.fill("#Password", a["password"])
        page.click("#btnSubmit")
        try:
            page.wait_for_url(lambda u: LOGIN_PATH.lower() not in u.lower(), timeout=30000)
        except Exception:
            _rejected[0] = True
            raise RuntimeError("Udvash didn't accept the login" + _page_message(page)
                               + ". Check the registration number and password and save them again") from None


def _relay(route, request):
    """The feeder script's request to the app (RELAY/...): pass it on and hand back the answer."""
    import urllib.error
    import urllib.request
    url = APP[0] + request.url[len(RELAY):]
    req = urllib.request.Request(url, data=request.post_data_buffer, method=request.method,
                                 headers={"Content-Type": request.headers.get("content-type", "application/json")})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, body, ctype = r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        status, body, ctype = e.code, e.read(), e.headers.get("Content-Type", "text/plain")
    except Exception as e:
        status, body, ctype = 502, str(e).encode(), "text/plain"
    route.fulfill(status=status, body=body, content_type=ctype)


def _pause(b, seconds):
    """Wait, while letting the browser's requests to the app through (they're only handled while
    this thread is inside a Playwright call)."""
    end = time.time() + seconds
    while time.time() < end and not _wake.is_set():
        if not b.page:
            _wake.wait(end - time.time())
            break
        try:
            b.page.wait_for_timeout(200)
        except Exception:
            break


def _page_message(page):
    """The message a failed login shows (wrong password, ...), if one is visible."""
    try:
        text = page.evaluate("""() => [...document.querySelectorAll(
              '.text-danger, .field-validation-error, .validation-summary-errors, .alert, .swal2-html-container, .toast-message, .error')]
            .filter(e => e.offsetParent !== null).map(e => e.innerText.trim()).filter(Boolean).join(' ')""")
    except Exception:
        return ""
    return f": {text[:200]}" if text else ""


def _run():
    from . import course, downloader
    b = _Browser()
    idle_since = 0.0
    while True:
        _pause(b, 10)
        _wake.clear()
        try:
            if _forget.is_set():
                _forget.clear()
                b.close()
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
                _set("off")
            if _rejected[0]:
                continue  # stays "error" with Udvash's message until the account is saved again
            if not configured():
                if b.running():
                    b.close()
                _set("off")
                continue
            testing = _test.is_set()
            if needed() or testing:
                idle_since = 0.0
                seen = downloader.feeder["last_seen"]
                quiet = not seen or time.time() - seen > REVIVE_AFTER
                if not b.running():
                    _set("starting")
                    b.launch()
                if not b.page or (quiet and time.time() - b.opened > REVIVE_AFTER):
                    b.open_feeder(course.start_url() or course.PORTAL + "/")
                _test.clear()
                _set("connected")
            elif b.running():
                idle_since = idle_since or time.time()
                if time.time() - idle_since > IDLE_CLOSE:
                    b.close()
                    _set("off")
        except Exception as e:
            log.warning("Udvash browser: %s", e)
            _test.clear()
            b.close()
            _set("error", str(e).splitlines()[0][:300])
            _wake.wait(60)  # don't hammer Udvash's login with retries
            _wake.clear()
