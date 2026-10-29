"""Background download workers (run inside the Django server process)."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

import yt_dlp
from django.conf import settings
from django.db import close_old_connections

from .models import Lecture

_lock = threading.Lock()
_started = False
pdf_job = {"running": False, "total": 0, "done": 0, "failed": 0}

# How many downloads run at once. Every worker thread exists from the start; worker i only
# takes new work while i < limit, so changing a limit takes effect without a restart.
MAX_WORKERS = 8
LIMITS_FILE = settings.BASE_DIR / "download_limits.json"


def _load_limits():
    limits = {"pdf": 2, "video": settings.DOWNLOAD_WORKERS}
    try:
        limits.update({k: max(1, min(MAX_WORKERS, int(v)))
                       for k, v in json.loads(LIMITS_FILE.read_text()).items() if k in limits})
    except (OSError, ValueError):
        pass
    return limits


limits = _load_limits()


def set_limits(pdf=None, video=None):
    for key, value in (("pdf", pdf), ("video", video)):
        if value is not None:
            limits[key] = max(1, min(MAX_WORKERS, int(value)))
    LIMITS_FILE.write_text(json.dumps(limits))
    return dict(limits)


def ensure_started():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    Lecture.objects.filter(status="downloading").update(status="queued")  # resume after a restart
    for i in range(MAX_WORKERS):
        threading.Thread(target=_worker, args=(i,), name=f"video-{i}", daemon=True).start()
        threading.Thread(target=_pdf_worker, args=(i,), name=f"pdf-{i}", daemon=True).start()
    threading.Thread(target=_priority_worker, name="pdf-priority", daemon=True).start()


# Portal-only videos (no YouTube id) come from signed storage links that last an hour, like the
# notes. The link feeder fetches one from the lecture's video card when a queued video needs it.
VIDEO_LINK_MARGIN = 300  # seconds a video link must still have left to start (or resume) with it
WAITING_FOR_LINK = "Waiting for a fresh video link from the Udvash tab"
_video_link_asked = {}  # pk -> when the feeder was last asked for its video link


class _Relink(Exception):
    """The video link expired mid-download: wait for a new one and resume from the .part file."""


def _video_link_ok(lec):
    if lec.youtube_id:
        return True
    expires = link_expiry(lec.direct_url)
    return bool(lec.direct_url) and (not expires or expires - time.time() > VIDEO_LINK_MARGIN)


def _claim():
    with _lock:
        for lec in Lecture.objects.filter(status="queued").order_by("queued_at", "order"):
            if not _video_link_ok(lec):
                if lec.error != WAITING_FOR_LINK:
                    Lecture.objects.filter(pk=lec.pk).update(error=WAITING_FOR_LINK)
                continue
            Lecture.objects.filter(pk=lec.pk).update(status="downloading", progress=0, speed="", error="")
            return lec
        return None


def video_links_needed():
    """For the link feeder: queued portal-only videos that need a fresh link, with their video card."""
    now = time.time()
    feeder["last_seen"] = now
    out = []
    for lec in Lecture.objects.filter(status="queued", youtube_id="").exclude(portal_url=""):
        if not _video_link_ok(lec) and now - _video_link_asked.get(lec.pk, 0) > LINK_RETRY_AFTER:
            _video_link_asked[lec.pk] = now
            card = lec.portal_url.split("udvash-unmesh.com", 1)[-1]  # same-site path for the feeder
            out.append({"pk": lec.pk, "card": card})
    return out


def _worker(i):
    while True:
        if i >= limits["video"]:
            time.sleep(1)
            continue
        close_old_connections()
        lec = _claim()
        if not lec:
            time.sleep(2)
            continue
        try:
            _download(lec)
            Lecture.objects.filter(pk=lec.pk, status="downloading").update(status="done", progress=100, speed="")
        except _Relink:
            _video_link_asked.pop(lec.pk, None)  # ask for the new link right away
            Lecture.objects.filter(pk=lec.pk, status="downloading").update(
                status="queued", error=WAITING_FOR_LINK, speed="")
        except Exception as e:
            # A cancelled download is no longer "downloading", so this leaves it alone.
            Lecture.objects.filter(pk=lec.pk, status="downloading").update(
                status="failed", error=str(e)[:2000], speed="")


def _human_speed(bps):
    if not bps:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.1f} {unit}/s"
        bps /= 1024
    return f"{bps:.1f} TB/s"


# Udvash's storage firewall resets Python/OpenSSL connections within milliseconds but serves
# Windows' own curl.exe (Schannel TLS) at full speed, so note PDFs are fetched with curl.
CURL = shutil.which("curl", path=r"C:\Windows\System32") or shutil.which("curl")


def link_works(url):
    """Does this signed storage link answer right now? (Asks for its first byte, with curl.)"""
    try:
        out = subprocess.run([CURL, "-s", "-o", os.devnull, "-r", "0-0", "--connect-timeout", "10", "--max-time", "20",
                              "-w", "%{http_code}", url], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out in ("200", "206")


def first_working(urls):
    """The first link that works, trying them in order (Bangla card first, then English). If none
    answers (the storage server is down), the first one: it's retried later like any other."""
    for url in urls:
        if link_works(url):
            return url
    return urls[0] if urls else ""


def _remote_size(url):
    """Total size of the file behind url, from a 1-byte range request (0 if unknown)."""
    try:
        out = subprocess.run([CURL, "-sS", "-r", "0-0", "-o", os.devnull, "-D", "-", "--max-time", "10", url],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        m = re.search(r"Content-Range:\s*bytes\s+\d+-\d+/(\d+)", out, re.I)
        return int(m[1]) if m else 0
    except (OSError, subprocess.SubprocessError):
        return 0


def _fail(lec, reason):
    pdf_job["error"] = reason
    _pdf_failed[lec.pk] = reason
    return False


def fetch_note(lec, cancellable=False):
    """Save the lecture's note PDF. True if it is on disk afterwards, False on failure,
    None if a queued download was cancelled (cancellable=True only)."""
    lec.refresh_from_db(fields=["note_url"])  # the link may have been refreshed since the job started
    if lec.pdf_saved:
        return True
    if not lec.note_url:
        return _fail(lec, "no note link")
    lec.pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lec.pdf_path.with_name(lec.pdf_path.name + ".part")
    info = {"id": lec.pk, "title": lec.title[:120], "got": 0, "size": 0, "note": "connecting…"}
    with _lock:
        _pdf_active[lec.pk] = info
    tries = 8  # consecutive attempts that get no new bytes before giving up
    stalls = 0
    try:
        if not CURL:
            return _fail(lec, "curl.exe not found")
        size = _remote_size(lec.note_url)
        info["size"] = size
        # Keep the .part file between attempts; curl's "-C -" resumes it from its current size.
        while stalls < tries:
            have = tmp.stat().st_size if tmp.exists() else 0
            if size and have >= size:
                tmp.replace(lec.pdf_path)
                _pdf_failed.pop(lec.pk, None)
                return True
            info.update(got=have, note="")
            proc = subprocess.Popen(
                [CURL, "-sS", "-f", "--connect-timeout", "20", "--speed-limit", "1024", "--speed-time", "30",
                 "-C", "-", "-o", str(tmp), "-w", "%{http_code}", lec.note_url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while proc.poll() is None:
                time.sleep(0.5)
                info["got"] = tmp.stat().st_size if tmp.exists() else 0
                if cancellable and lec.pk not in _pdf_pending:
                    proc.kill()
                    return None  # keep the .part file so a later download resumes it
            code, err = proc.communicate()
            got = tmp.stat().st_size if tmp.exists() else 0
            info["got"] = got
            status = (code or "").strip()
            if proc.returncode == 0 or (status == "416" and got):  # 416: the .part file was already complete
                tmp.replace(lec.pdf_path)
                _pdf_failed.pop(lec.pk, None)
                return True
            if status == "403":
                # Keep the .part file: a fresh link resumes it from the same byte.
                if cancellable:
                    info["note"] = "link expired — waiting for a fresh link"
                    return RELINK
                return _fail(lec, "link expired (HTTP 403)")
            if status.startswith("4"):
                return _fail(lec, f"HTTP {status}")
            pdf_job["error"] = (err or f"curl exit {proc.returncode}").strip()[:200]
            if got > have:
                stalls = 0  # made progress: reconnect straight away
                info["note"] = f"connection dropped at {got / 1048576:.1f} MB — resuming…"
                time.sleep(0.5)
            else:
                stalls += 1
                if cancellable and got == 0 and stalls >= 2:
                    return DEFER  # can't even connect: let other files go first, retry this one later
                info["note"] = f"server not responding at {got / 1048576:.1f} MB — retry {stalls} of {tries}…"
                time.sleep(min(20, 2 * stalls))
        got = tmp.stat().st_size if tmp.exists() else 0
        return _fail(lec, f"server kept dropping the connection (stopped at {got / 1048576:.1f} MB, will resume)")
    finally:
        with _lock:
            _pdf_active.pop(lec.pk, None)


def pdf_status():
    """Snapshot of the PDF job, including the files being fetched right now."""
    with _lock:
        return {**pdf_job, "active": [dict(i) for i in _pdf_active.values()]}


# PDF queue. Signed note links only last an hour, so links are fetched just in time: the
# Udvash tab's link feeder asks links_needed() which PDFs need one, a batch at a time, and a
# worker only starts a PDF whose link is still valid.
RELINK = "relink"  # fetch_note result: the link expired mid-download, resume once a new one arrives
DEFER = "defer"  # fetch_note result: the storage host is unreachable, retry after DEFER_SECONDS
DEFER_SECONDS = 600
LINK_BATCH = 10  # links the feeder fetches per request
LINK_LOW_WATER = 2  # ask for the next batch when fewer queued PDFs than this have a usable link
LINK_MARGIN = 60  # seconds a link must still have left to start (or resume) a download
LINK_RETRY_AFTER = 60  # don't ask for the same PDF's link again sooner than this

_pdf_order = deque()  # pending lecture pks waiting for a worker, in download order
_pdf_pending = set()  # lecture pks queued or in progress
_pdf_lectures = {}  # pk -> Lecture, for pending PDFs
_pdf_inflight = set()  # lecture pks a worker is fetching
_pdf_active = {}  # pk -> progress of the PDFs being fetched right now
_pdf_failed = {}  # pk -> why its last download failed
_link_expires = {}  # pk -> unix time its current note link stops working
_link_asked = {}  # pk -> when the feeder was last asked for its link
_priority = set()  # pks someone opened: downloaded first, in the priority slot
_relink = set()  # pks whose link expired mid-download: asked for first, without waiting for a batch
_host_down = {}  # storage host -> unix time to try it again (it refused connections)


def _host(url):
    m = re.match(r"https://([^/]+)/", url or "")
    return m[1] if m else ""


def _retry_at(pk):
    """When a queued PDF may be tried again because its storage host was down (0 = now)."""
    until = _host_down.get(_host(_pdf_lectures[pk].note_url), 0) if pk in _pdf_lectures else 0
    return until if until > time.time() else 0
feeder = {"last_seen": 0.0, "asked": 0, "received": 0}


def link_expiry(url):
    """Unix time a signed S3/R2 link expires (0 if it isn't one)."""
    date = re.search(r"X-Amz-Date=(\d{8}T\d{6})Z", url or "")
    secs = re.search(r"X-Amz-Expires=(\d+)", url or "")
    if not (date and secs):
        return 0
    signed = datetime.strptime(date[1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    return signed + int(secs[1])


def _usable(pk):
    return _link_expires.get(pk, 0) - time.time() > LINK_MARGIN


def downloads_snapshot():
    """Everything the Downloads page needs about PDFs, taken under the lock."""
    with _lock:
        return {
            "job": dict(pdf_job),
            "active": {pk: dict(i) for pk, i in _pdf_active.items()},
            "pending": set(_pdf_pending),
            "needs_link": {pk for pk in _pdf_pending if pk not in _pdf_inflight and not _usable(pk)},
            "deferred": {pk: t for pk in _pdf_pending if (t := _retry_at(pk))},
            "failed": dict(_pdf_failed),
            "feeder": dict(feeder),
        }


def cancel_pdfs(ids=None):
    """Remove PDFs from the queue (all of them if ids is None); a running one stops at its next chunk."""
    with _lock:
        targets = set(_pdf_pending) if ids is None else set(ids) & _pdf_pending
        _pdf_pending.difference_update(targets)
        for pk in targets:
            if pk in _pdf_order:
                _pdf_order.remove(pk)
        pdf_job["total"] -= len(targets)
        pdf_job["running"] = bool(_pdf_pending)
        return len(targets)


def start_pdf_job(ids):
    """Queue the note PDFs for these lectures. Adds to a running job. Returns how many were added."""
    with _lock:
        lectures = [lec for lec in Lecture.objects.filter(pk__in=ids).exclude(note_url="").order_by("order")
                    if lec.pk not in _pdf_pending and not lec.pdf_saved]
        if not pdf_job["running"]:
            pdf_job.update(total=0, done=0, failed=0, error="")
        for lec in lectures:
            _pdf_pending.add(lec.pk)
            _pdf_lectures[lec.pk] = lec
            _pdf_failed.pop(lec.pk, None)
            _link_expires[lec.pk] = link_expiry(lec.note_url)
            _pdf_order.append(lec.pk)
        pdf_job["total"] += len(lectures)
        pdf_job["running"] = bool(_pdf_pending)
    ensure_started()
    return len(lectures)


def open_pdf(pk):
    """Someone opened this lecture's note: fetch it before everything else, in its own slot."""
    start_pdf_job([pk])
    with _lock:
        if pk not in _pdf_pending:
            return
        _priority.add(pk)
        _pdf_failed.pop(pk, None)
        if pk in _pdf_order:
            _pdf_order.remove(pk)
            _pdf_order.appendleft(pk)
        if not _usable(pk) and pk not in _relink:
            _relink.add(pk)  # the feeder fetches these first
            _link_asked.pop(pk, None)


def links_updated(pks):
    """New note links were saved for these lectures: pending ones can start or resume."""
    with _lock:
        for lec in Lecture.objects.filter(pk__in=[pk for pk in pks if pk in _pdf_pending]):
            _link_expires[lec.pk] = link_expiry(lec.note_url)
            _pdf_lectures[lec.pk].note_url = lec.note_url  # its host may differ from the old link's
            if _usable(lec.pk):
                _relink.discard(lec.pk)
        feeder["received"] += len(pks)


def links_needed():
    """Pending PDFs the feeder should fetch links for: the next batch, once the usable ones run low."""
    with _lock:
        now = time.time()
        feeder["last_seen"] = now
        # PDFs on a storage host that's down are skipped: a link for them can't be used yet, and
        # counting them as "ready" would stop the feeder from fetching links for anything else.
        due = lambda pk: not _usable(pk) and not _retry_at(pk) and now - _link_asked.get(pk, 0) > LINK_RETRY_AFTER
        # Downloads interrupted by an expired link or a server that stopped answering go first,
        # whatever the batch state (the second kind at most once per DEFER_SECONDS).
        relink_due = lambda pk: not _usable(pk) and now - _link_asked.get(pk, 0) > (
            DEFER_SECONDS if _retry_at(pk) else LINK_RETRY_AFTER)
        batch = [pk for pk in _pdf_order if pk in _relink and relink_due(pk)]
        ready = sum(1 for pk in _pdf_order if _usable(pk) and not _retry_at(pk))
        if ready < max(LINK_LOW_WATER, limits["pdf"]):
            batch += [pk for pk in _pdf_order if pk not in _relink and due(pk)]
        batch = batch[:LINK_BATCH]
        for pk in batch:
            _link_asked[pk] = now
        feeder["asked"] += len(batch)
        return batch


def _take_pdf(priority_only=False):
    """Next pending PDF with a usable link, or None."""
    with _lock:
        for pk in _pdf_order:
            if priority_only and pk not in _priority:
                continue
            if _usable(pk) and not _retry_at(pk):
                _pdf_order.remove(pk)
                _pdf_inflight.add(pk)
                return _pdf_lectures[pk]
    return None


def _pdf_worker(i):
    # Daemon threads (not a ThreadPoolExecutor) so the dev server can still auto-reload mid-job.
    while True:
        if i >= limits["pdf"]:
            time.sleep(1)
            continue
        lec = _take_pdf()
        if not lec:
            time.sleep(1)  # nothing queued, or waiting for the feeder to bring links
            continue
        _run_pdf(lec)


def _priority_worker():
    # Its own slot, so a PDF someone just opened doesn't wait behind the running downloads.
    while True:
        lec = _take_pdf(priority_only=True)
        if lec:
            _run_pdf(lec)
        else:
            time.sleep(0.5)


def _run_pdf(lec):
        result = False
        try:
            close_old_connections()
            result = fetch_note(lec, cancellable=True)
        finally:
            with _lock:
                _pdf_inflight.discard(lec.pk)
                if result == RELINK and lec.pk in _pdf_pending:
                    _link_expires[lec.pk] = 0
                    _relink.add(lec.pk)
                    _link_asked.pop(lec.pk, None)  # ask for its new link right away
                    _pdf_order.appendleft(lec.pk)  # first in line once the new link arrives
                elif result == DEFER and lec.pk in _pdf_pending:
                    # Set aside every queued PDF on that host, not just this one...
                    _host_down[_host(lec.note_url)] = time.time() + DEFER_SECONDS
                    _pdf_order.append(lec.pk)  # back of the line
                    # ...and ask for fresh links now: the English card's copy may be on another server.
                    _link_expires[lec.pk] = 0
                    _relink.add(lec.pk)
                    _link_asked.pop(lec.pk, None)
                else:
                    _pdf_pending.discard(lec.pk)
                    _pdf_lectures.pop(lec.pk, None)
                    _relink.discard(lec.pk)
                    _priority.discard(lec.pk)
                    if result is not None:
                        pdf_job["done" if result else "failed"] += 1
                pdf_job["running"] = bool(_pdf_pending)


def _download(lec):
    lec.base_path.parent.mkdir(parents=True, exist_ok=True)
    if not lec.video_saved:
        _download_video(lec)
    fetch_note(lec)  # after the video: the note server can be slow and must not hold up a signed video link


def ytdl_opts(base, hook):
    """yt-dlp settings for a lecture video saved as <base>.mp4 (also used by to_device)."""
    q = settings.DOWNLOAD_QUALITY
    return {
        "format": f"bv*[height<={q}]+ba/b[height<={q}]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(base).replace("%", "%%") + ".%(ext)s",
        "ffmpeg_location": str(settings.FFMPEG_DIR),
        "js_runtimes": {"node": {"path": None}},
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }


def _download_video(lec):

    last = [0.0]

    def hook(d):
        now = time.monotonic()
        if d["status"] != "downloading" or now - last[0] < 1:
            return
        last[0] = now
        if not Lecture.objects.filter(pk=lec.pk, status="downloading").exists():
            raise RuntimeError("cancelled")
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        pct = d.get("downloaded_bytes", 0) * 100 / total if total else 0
        Lecture.objects.filter(pk=lec.pk).update(progress=round(pct, 1), speed=_human_speed(d.get("speed")))

    if not lec.youtube_id:
        return _download_direct(lec)
    with yt_dlp.YoutubeDL(ytdl_opts(lec.base_path, hook)) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={lec.youtube_id}"])


def _download_direct(lec):
    """A portal-only video from Udvash storage, with curl (see CURL): resumable via a .part file."""
    path = lec.video_path
    tmp = path.with_name(path.name + ".part")
    url = lec.direct_url
    size = _remote_size(url)
    stalls = 0
    while stalls < 8:
        before = tmp.stat().st_size if tmp.exists() else 0
        proc = subprocess.Popen([CURL, "-sS", "-f", "--connect-timeout", "20", "--speed-limit", "1024",
                                 "--speed-time", "30", "-C", "-", "-o", str(tmp), "-w", "%{http_code}", url],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        last, t0 = before, time.monotonic()
        while proc.poll() is None:
            time.sleep(1)
            got = tmp.stat().st_size if tmp.exists() else 0
            now = time.monotonic()
            if not Lecture.objects.filter(pk=lec.pk, status="downloading").exists():
                proc.kill()
                raise RuntimeError("cancelled")
            Lecture.objects.filter(pk=lec.pk).update(
                progress=round(got * 100 / size, 1) if size else 0,
                speed=_human_speed((got - last) / (now - t0)) if now > t0 else "")
            last, t0 = got, now
        out, err = proc.communicate()
        code = (out or "").strip()[-3:]
        got = tmp.stat().st_size if tmp.exists() else 0
        if proc.returncode == 0 or (code == "416" and got):
            tmp.replace(path)
            return
        if code == "403":
            raise _Relink()
        if code.startswith("4") and code != "416":
            raise RuntimeError(f"HTTP {code} from video storage")
        if got > before:
            stalls = 0
        else:
            stalls += 1
            time.sleep(min(60, 5 * stalls))
    raise RuntimeError(f"video storage not responding ({(err or '').strip()[:200]})")
