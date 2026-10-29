"""Downloads to the device the browser is on ("📥" buttons), without adding anything to the library.

- Already saved on this PC: sent from here.
- Udvash notes and portal-only videos: the link feeder fetches a fresh signed link, and the browser
  downloads it from Udvash's storage itself. This PC isn't in the way at all.
- YouTube videos: they have to be fetched and merged on this PC first (yt-dlp + ffmpeg), into a
  temporary folder. The copy is deleted once the device has received all of it, or after TEMP_TTL.
"""
import re
import shutil
import threading
import time
from urllib.parse import quote

import yt_dlp
from django.conf import settings
from django.urls import reverse

from . import downloader
from .models import Lecture

TEMP_DIR = settings.DOWNLOAD_DIR / "_to_device"
TEMP_TTL = 3 * 3600  # an unfinished transfer's copy is kept this long after its last use
LINK_WANT_FOR = 600  # keep asking the feeder for a link this long after the page last asked

_lock = threading.Lock()
_want = {}  # (pk, "pdf" | "video") -> when a device page last asked for it
_asked = {}  # (pk, kind) -> when the feeder was last asked for its link
_jobs = {}  # pk -> YouTube video being prepared here: {state, pct, speed, error, path, size, served, streams, used}
_started = False


def _ensure_started():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    shutil.rmtree(TEMP_DIR, ignore_errors=True)  # copies left over from before a restart
    threading.Thread(target=_cleaner, name="to-device-cleaner", daemon=True).start()


def link_ok(url, expiry=None):
    expires = downloader.link_expiry(url) if expiry is None else expiry
    return bool(url) and (not expires or expires - time.time() > downloader.LINK_MARGIN)


def status(lec, kind):
    """What the device page should do next: {"state": "ready", "url"} or a reason to wait."""
    _ensure_started()
    if kind == "stream":  # the Watch page, for a lecture that isn't on YouTube
        if lec.youtube_id:
            return {"state": "youtube"}
        if lec.video_saved:
            return {"state": "ready", "url": reverse("file", args=[lec.pk, "video"])}
        return _direct(lec, "video", lec.direct_url)
    if kind == "video360":
        if not lec.youtube_id:
            return {"state": "failed", "error": "Only YouTube lectures have a 360p direct download."}
        return _youtube_360(lec)
    if kind == "pdf":
        if lec.pdf_saved:
            return {"state": "ready", "url": reverse("file", args=[lec.pk, "pdf"]) + "?dl=1"}
        if not lec.note_url:
            return {"state": "failed", "error": "This lecture has no class note."}
        return _direct(lec, kind, lec.note_url)
    if lec.video_saved:
        return {"state": "ready", "url": reverse("file", args=[lec.pk, "video"]) + "?dl=1"}
    if lec.youtube_id:
        return _youtube(lec)
    if not lec.portal_url:
        return {"state": "failed", "error": "This video has no download link."}
    return _direct(lec, kind, lec.direct_url)


def _direct(lec, kind, url):
    if link_ok(url):
        with _lock:
            _want.pop((lec.pk, kind), None)
        return {"state": "ready", "url": url, "direct": True,
                "relay": reverse("get_relay", args=[lec.pk, kind])}
    with _lock:
        _want[(lec.pk, kind)] = time.time()
    seen = downloader.feeder["last_seen"]
    return {"state": "link", "feeder_ago": round(time.time() - seen) if seen else None}


def waiting_for_links():
    """True while a device page is waiting on a Udvash link (no side effects)."""
    now = time.time()
    with _lock:
        return any(now - t <= LINK_WANT_FOR for t in _want.values())


def links_wanted(kind):
    """For the feeder: lectures a device is waiting on a fresh link for (asked at most once a minute)."""
    now = time.time()
    out = []
    with _lock:
        for (pk, k), t in list(_want.items()):
            if now - t > LINK_WANT_FOR:
                _want.pop((pk, k), None)
            elif k == kind and now - _asked.get((pk, k), 0) > downloader.LINK_RETRY_AFTER:
                _asked[(pk, k)] = now
                out.append(pk)
    return out


# ---- YouTube 360p: the one quality YouTube has as a single file with sound ----
# This PC only works the link out (yt-dlp); the device downloads it from YouTube itself. YouTube
# ties the link to the internet address that asked for it, so it works on devices on the same
# home connection. Its title= parameter makes YouTube send it as a download under that name.

_links360 = {}  # pk -> {state, url, error}


def _youtube_360(lec):
    with _lock:
        job = _links360.get(lec.pk)
        expired = job and job["state"] == "ready" and not link_ok(job["url"], expiry=_yt_expiry(job["url"]))
        if not job or expired:
            job = _links360[lec.pk] = {"state": "resolving", "url": "", "error": ""}
            threading.Thread(target=_resolve_360, args=(lec, job), name=f"yt360-{lec.pk}", daemon=True).start()
        if job["state"] == "failed":
            _links360.pop(lec.pk, None)  # reopening the page tries again
            return {"state": "failed", "error": job["error"]}
        if job["state"] == "ready":
            return {"state": "ready", "url": job["url"], "direct": True}
        return {"state": "resolving"}


def _resolve_360(lec, job):
    opts = {"format": "18/b[height<=360][acodec!=none][vcodec!=none]",
            "quiet": True, "no_warnings": True, "js_runtimes": {"node": {"path": None}}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={lec.youtube_id}", download=False)
        job.update(state="ready", url=info["url"] + "&title=" + quote(lec.base_path.name + " 360p"))
    except Exception as e:
        job.update(state="failed", error=str(e)[:300])


def _yt_expiry(url):
    m = re.search(r"[?&]expire=(\d+)", url)
    return int(m[1]) if m else 0


# ---- YouTube: prepared here, then handed over and deleted ----

def _youtube(lec):
    with _lock:
        job = _jobs.get(lec.pk)
        if job and job["state"] == "failed" and time.time() - job["used"] > 5:
            job = None  # reopening the page tries again
        if not job:
            job = _jobs[lec.pk] = {"state": "preparing", "pct": 0, "speed": "", "error": "", "path": None,
                                   "size": 0, "served": 0, "streams": 0, "used": time.time()}
            threading.Thread(target=_prepare, args=(lec, job), name=f"to-device-{lec.pk}", daemon=True).start()
        job["used"] = time.time()
        if job["state"] == "ready":
            return {"state": "ready", "url": reverse("get_file", args=[lec.pk])}
        return {k: job[k] for k in ("state", "pct", "speed", "error")}


def _prepare(lec, job):
    folder = TEMP_DIR / str(lec.pk)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    last = [0.0]

    def hook(d):
        now = time.monotonic()
        if d["status"] != "downloading" or now - last[0] < 1:
            return
        last[0] = now
        if time.time() - job["used"] > 120:
            raise RuntimeError("nobody is waiting for it any more")  # the page was closed
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        job.update(pct=round(d.get("downloaded_bytes", 0) * 100 / total, 1) if total else 0,
                   speed=downloader._human_speed(d.get("speed")))

    try:
        base = folder / lec.base_path.name
        with yt_dlp.YoutubeDL(downloader.ytdl_opts(base, hook)) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={lec.youtube_id}"])
        path = base.with_name(base.name + ".mp4")
        job.update(state="ready", path=path, size=path.stat().st_size, pct=100, speed="", used=time.time())
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        job.update(state="failed", error=str(e)[:300], used=time.time())


def prepared_file(pk):
    """The prepared copy of a YouTube video, or None."""
    with _lock:
        job = _jobs.get(pk)
        if job and job["state"] == "ready" and job["path"] and job["path"].exists():
            job["used"] = time.time()
            return job
    return None


def counting(job, chunks):
    """Wrap a file stream so the copy is deleted once the device has had every byte."""
    with _lock:
        job["streams"] += 1
    try:
        for chunk in chunks:
            job["served"] += len(chunk)
            job["used"] = time.time()
            yield chunk
    finally:
        with _lock:
            job["streams"] -= 1


def _cleaner():
    while True:
        time.sleep(30)
        now = time.time()
        with _lock:
            for pk, job in list(_jobs.items()):
                idle = now - job["used"]
                finished = job["state"] == "ready" and job["served"] >= job["size"] > 0
                if job["streams"] or job["state"] == "preparing":
                    continue
                if (finished and idle > 30) or idle > TEMP_TTL or (job["state"] == "failed" and idle > 300):
                    shutil.rmtree(TEMP_DIR / str(pk), ignore_errors=True)
                    _jobs.pop(pk, None)
