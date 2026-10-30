import json
import re
import subprocess
import time

from django.conf import settings
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import course, downloader, to_device
from .importer import sync_files
from .models import Lecture


def index(request):
    downloader.ensure_started()
    here = course.lectures()  # the current course

    lectures = here.all()
    f = {k: request.GET.get(k, "") for k in ("q", "subject", "type", "status")}
    if f["q"]:
        lectures = lectures.filter(Q(title__icontains=f["q"]) | Q(chapter__icontains=f["q"]))
    if f["subject"]:
        lectures = lectures.filter(subject=f["subject"])
    if f["type"]:
        lectures = lectures.filter(class_type=f["type"])
    if f["status"] == "none":
        lectures = lectures.filter(status="")
    elif f["status"]:
        lectures = lectures.filter(status=f["status"])

    counts = dict(here.values_list("status").annotate(n=Count("id")))
    return render(request, "library/index.html", {
        "lectures": lectures,
        "f": f,
        # dict.fromkeys keeps course order; .distinct() with order_by("order") would repeat each subject per lecture.
        "subjects": list(dict.fromkeys(here.order_by("order").values_list("subject", flat=True))),
        "types": here.order_by("class_type").values_list("class_type", flat=True).distinct(),
        "total": here.count(),
        "done": sum(lec.video_saved for lec in here.exclude(status="")),  # playable from this PC
        "active": counts.get("queued", 0) + counts.get("downloading", 0),
        "failed": counts.get("failed", 0),
        "quality": settings.DOWNLOAD_QUALITY,
        "video_total": here.exclude(youtube_id="", portal_url="", direct_url="").count(),
        "pdf_total": here.exclude(note_url="").count(),
        "course_id": course.current(),
        "pdf_saved": sum(lec.pdf_saved for lec in here.exclude(note_url="")),
        "course_start": course.start_url(),
        "feeder_ext_dir": settings.BASE_DIR.parent / "feeder-extension",
    })


def watch(request, pk):
    lec = get_object_or_404(Lecture, pk=pk)
    siblings = list(Lecture.objects.filter(subject=lec.subject, chapter=lec.chapter))
    i = next(n for n, s in enumerate(siblings) if s.pk == lec.pk)
    local = lec.video_saved  # saved on this PC
    return render(request, "library/watch.html", {
        "lec": lec,
        "siblings": siblings,
        "prev": siblings[i - 1] if i > 0 else None,
        "next": siblings[i + 1] if i + 1 < len(siblings) else None,
        "local": local,
        # Prefer the downloaded file; ?mode=online switches back to the YouTube embed.
        "offline": local and (request.GET.get("mode") != "online" or not lec.youtube_id),
    })


def note(request, pk):
    """Open the class note: the downloaded PDF if we have it, otherwise the online link."""
    lec = get_object_or_404(Lecture, pk=pk)
    if lec.pdf_saved:
        return render(request, "library/note_view.html", {"lec": lec})
    if not lec.note_url:
        raise Http404("This lecture has no class note")
    if request.GET.get("online"):
        return redirect(lec.note_url)
    # Not on disk yet: download it first (ahead of the queue), then show it here.
    downloader.open_pdf(pk)
    return render(request, "library/note_wait.html", {"lec": lec})


def api_note_state(request, pk):
    """Progress of one note PDF, for the page shown while an opened note downloads."""
    lec = get_object_or_404(Lecture, pk=pk)
    snap = downloader.downloads_snapshot()
    seen = snap["feeder"]["last_seen"]
    return JsonResponse({**(_pdf_row(lec, snap) or {"state": "none"}),
                         "feeder_ago": round(time.time() - seen) if seen else None})


@require_POST
def set_direct_url(request, pk):
    lec = get_object_or_404(Lecture, pk=pk)
    lec.direct_url = request.POST.get("url", "").strip()
    lec.save(update_fields=["direct_url"])
    return redirect("watch", pk=pk)


def serve_file(request, pk, kind):
    lec = get_object_or_404(Lecture, pk=pk)
    if kind == "video":
        path, ctype = lec.video_path, "video/mp4"
    elif kind == "pdf":
        path, ctype = lec.pdf_path, "application/pdf"
        if request.GET.get("as") == "data":
            # For the in-app viewer's fetch(): download managers (IDM) grab anything served as a PDF,
            # even background requests, so hand over the same bytes under a type they ignore.
            ctype = "text/plain; charset=x-user-defined"
    else:
        raise Http404
    if request.GET.get("dl"):
        ctype = "application/octet-stream"
    if not path.exists():
        raise Http404("File not downloaded yet")
    resp = _serve_path(request, path, ctype)
    if request.GET.get("dl"):
        resp["Content-Disposition"] = content_disposition_header(True, path.name)
    return resp


def _serve_path(request, path, ctype, wrap=lambda chunks: chunks):
    """A local file, with byte-range support so the browser can seek in long videos.
    wrap() sees every chunk sent (to_device counts them)."""
    size = path.stat().st_size
    m = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    whole = not m or not (m[1] or m[2])
    if whole:
        start, end = 0, size - 1
    elif m[1]:
        start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
    else:
        start, end = max(size - int(m[2]), 0), size - 1

    def stream(remaining=end - start + 1):
        with open(path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    # No Content-Disposition on a plain response: for the in-app viewer (?as=data) a ".pdf"
    # filename is another thing IDM reacts to.
    resp = StreamingHttpResponse(wrap(stream()), status=200 if whole else 206, content_type=ctype)
    if not whole:
        resp["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp["Content-Length"] = str(end - start + 1)
    resp["Accept-Ranges"] = "bytes"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


def get_page(request, pk, kind):
    """📥: download this lecture's video or note to the device that opened the page."""
    if kind not in ("video", "video360", "pdf"):
        raise Http404
    return render(request, "library/get.html", {"lec": get_object_or_404(Lecture, pk=pk), "kind": kind})


def get_bulk(request):
    """📥 for many lectures: ?kind=video|video360|pdf&ids=1,2,3 (only those that have one are listed)."""
    kind = request.GET.get("kind", "")
    if kind not in ("video", "video360", "pdf"):
        raise Http404
    ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.isdigit()]
    has = {
        "video360": lambda lec: bool(lec.youtube_id),
        "video": lambda lec: bool(lec.youtube_id or lec.portal_url) or lec.video_saved,
        "pdf": lambda lec: bool(lec.note_url) or lec.pdf_saved,
    }[kind]
    items = [{"id": lec.pk, "title": f"{lec.order:03d} {lec.title}"}
             for lec in Lecture.objects.filter(pk__in=ids) if has(lec)]
    return render(request, "library/get_bulk.html", {"kind": kind, "items": items, "skipped": len(ids) - len(items)})


def api_get(request, pk, kind):
    if kind not in ("video", "video360", "pdf", "stream"):
        raise Http404
    return JsonResponse(to_device.status(get_object_or_404(Lecture, pk=pk), kind))


def get_file(request, pk):
    """A YouTube video prepared for a device; deleted here once the device has all of it."""
    lec = get_object_or_404(Lecture, pk=pk)
    job = to_device.prepared_file(pk)
    if not job:
        raise Http404("Not prepared (or already handed over) - open the download page again")
    resp = _serve_path(request, job["path"], "application/octet-stream",
                       lambda chunks: to_device.counting(job, chunks))
    resp["Content-Disposition"] = content_disposition_header(True, lec.video_path.name)
    return resp


def get_relay(request, pk, kind):
    """Fallback when the browser shows a direct link instead of saving it: pass the bytes through
    this PC (with curl, see downloader.CURL) under the lecture's file name. Nothing is stored."""
    lec = get_object_or_404(Lecture, pk=pk)
    url = lec.note_url if kind == "pdf" else lec.direct_url
    if kind not in ("video", "pdf") or not to_device.link_ok(url):
        raise Http404("No fresh link - open the download page again")
    size = downloader._remote_size(url)
    proc = subprocess.Popen([downloader.CURL, "-sS", "-f", "--connect-timeout", "20", url],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def chunks():
        try:
            while data := proc.stdout.read(1 << 20):
                yield data
        finally:
            proc.kill()

    resp = StreamingHttpResponse(chunks(), content_type="application/octet-stream")
    if size:
        resp["Content-Length"] = str(size)
    name = (lec.pdf_path if kind == "pdf" else lec.video_path).name
    resp["Content-Disposition"] = content_disposition_header(True, name)
    return resp


def _ids(request):
    return [int(i) for i in json.loads(request.body or "{}").get("ids", [])]


@require_POST
def api_queue(request):
    downloader.ensure_started()
    sync_files()
    n = (Lecture.objects.filter(pk__in=_ids(request), status__in=["", "failed"])
         .exclude(youtube_id="", direct_url="")
         .update(status="queued", progress=0, error="", queued_at=timezone.now()))
    return JsonResponse({"queued": n})


@require_POST
def api_cancel(request):
    ids = _ids(request)
    qs = Lecture.objects.filter(status__in=["queued", "downloading"])
    if ids:
        qs = qs.filter(pk__in=ids)
    return JsonResponse({"cancelled": qs.update(status="", progress=0, speed="")})


def api_limits(request):
    """GET or POST {"pdf": n, "video": n}: how many downloads of each kind run at once."""
    if request.method == "POST":
        data = json.loads(request.body or "{}")
        return JsonResponse(downloader.set_limits(pdf=data.get("pdf"), video=data.get("video")))
    return JsonResponse(downloader.limits)


def _file_info(path):
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0


def _pdf_row(lec, snap):
    """Where a lecture's note PDF stands: active, queued, done or failed (None if never queued)."""
    pdf = lec.pdf_path
    size, mtime = _file_info(pdf)
    part_size, _ = _file_info(pdf.with_name(pdf.name + ".part"))
    if lec.pk in snap["active"]:
        a = snap["active"][lec.pk]
        return {"state": "active", "got": a["got"], "size": a["size"], "note": a["note"]}
    if lec.pk in snap["pending"]:
        return {"state": "queued", "got": part_size, "needs_link": lec.pk in snap["needs_link"],
                "retry_at": snap["deferred"].get(lec.pk)}
    if size:
        return {"state": "done", "size": size, "mtime": mtime}
    if lec.pk in snap["failed"]:
        return {"state": "failed", "got": part_size, "error": snap["failed"][lec.pk]}
    return None


@require_POST
def api_downloads_action(request):
    data = json.loads(request.body or "{}")
    action, kind = data.get("action"), data.get("kind")
    ids = [int(i) for i in data.get("ids", [])]
    if action == "retry" and kind == "pdf":
        return JsonResponse({"n": downloader.start_pdf_job(ids)})
    if action == "retry" and kind == "video":
        n = Lecture.objects.filter(pk__in=ids, status="failed").update(
            status="queued", progress=0, error="", queued_at=timezone.now())
        return JsonResponse({"n": n})
    if action == "cancel" and kind == "pdf":
        return JsonResponse({"n": downloader.cancel_pdfs(ids or None)})
    if action == "cancel" and kind == "video":
        qs = Lecture.objects.filter(status__in=["queued", "downloading"])
        return JsonResponse({"n": (qs.filter(pk__in=ids) if ids else qs).update(status="", progress=0, speed="")})
    if action == "reveal" and ids:
        lec = get_object_or_404(Lecture, pk=ids[0])
        path = lec.pdf_path if kind == "pdf" else lec.video_path
        if path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])  # local app: open Explorer on the file
            return JsonResponse({"n": 1})
        return JsonResponse({"n": 0})
    return JsonResponse({"error": "unknown action"}, status=400)


CONTENT_ID = re.compile(r"CustomizedContent_(\d+)/")
PORTAL_ORIGIN = "https://online.udvash-unmesh.com"


def _portal_cors(resp):
    """Let the logged-in Udvash tab (the link feeder) call these endpoints on localhost."""
    resp["Access-Control-Allow-Origin"] = PORTAL_ORIGIN
    resp["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp["Access-Control-Allow-Headers"] = "Content-Type"
    resp["Access-Control-Allow-Private-Network"] = "true"
    return resp


STORAGE_URL = re.compile(r"https://(storage-r\d+\.udvash-unmesh\.com|[0-9a-f]{32}\.r2\.cloudflarestorage\.com)/")


@csrf_exempt
def api_video_links(request):
    """GET: portal-only videos the link feeder should fetch a link for ({"videos": [{"pk", "card"}]}).
    POST {"links": [{"pk": 201, "url": "https://storage-rN.../....mp4?X-Amz-..."}]}: save them."""
    if request.method == "OPTIONS":
        return _portal_cors(HttpResponse())
    if request.method == "GET":
        wanted = [{"pk": lec.pk, "card": lec.portal_url.split("udvash-unmesh.com", 1)[-1]}
                  for lec in Lecture.objects.filter(pk__in=to_device.links_wanted("video")).exclude(portal_url="")]
        return _portal_cors(JsonResponse({"videos": wanted + downloader.video_links_needed()}))
    items = []
    for item in json.loads(request.body or "{}").get("links", []):
        # Udvash has put this class on YouTube since: remember it for good (YouTube ids don't expire),
        # and from now on it plays and downloads like every other YouTube lecture.
        yt = str(item.get("youtube_id") or "")
        if re.fullmatch(r"[\w-]{11}", yt):
            Lecture.objects.filter(pk=int(item.get("pk", 0)), youtube_id="").update(youtube_id=yt, error="")
        urls = [str(u).split("#")[0] for u in (item.get("urls") or [item.get("url", "")])]
        urls = [u for u in urls if STORAGE_URL.match(u) and ".mp4" in u.split("?")[0].lower()]
        if urls:
            items.append((int(item.get("pk", 0)), urls))
    saved = 0
    for pk, url in zip([pk for pk, _ in items], _pick_working([urls for _, urls in items])):
        saved += Lecture.objects.filter(pk=pk, youtube_id="").update(direct_url=url, error="")
    return _portal_cors(JsonResponse({"saved": saved}))


def _pick_working(url_lists):
    """For each list of links (Bangla card's first), the first that works; checked side by side."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(downloader.first_working, url_lists))


def api_feeder_wanted(request):
    """For the feeder extension's background part: should the Udvash feeder tab be open right now?
    Only a yes/no, so any origin may ask (the extension calls it from its own origin)."""
    needed = (course.wanted() or bool(downloader.downloads_snapshot()["needs_link"]) or to_device.waiting_for_links()
              or any(not downloader._video_link_ok(lec)
                     for lec in Lecture.objects.filter(status="queued", youtube_id="").exclude(portal_url="")))
    seen = downloader.feeder["last_seen"]
    resp = JsonResponse({"needed": needed, "start": course.start_url(),
                         "feeder_ago": round(time.time() - seen) if seen else None})
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Access-Control-Allow-Private-Network"] = "true"
    return resp


@csrf_exempt
def api_links_needed(request):
    """For the link feeder: portal content ids of the next batch of queued PDFs that need a fresh link."""
    if request.method == "OPTIONS":
        return _portal_cors(HttpResponse())
    pks = to_device.links_wanted("pdf") + downloader.links_needed()
    ids = []
    for lec in Lecture.objects.filter(pk__in=pks):
        m = CONTENT_ID.search(lec.note_url)
        if m:
            ids.append(m[1])
    return _portal_cors(JsonResponse({"content_ids": ids}))


@csrf_exempt
def api_note_links(request):
    """Receive fresh signed note links collected in the logged-in portal tab.

    Body: {"links": [{"content_id": "<id>", "urls": [<Bangla card's link>, <English card's link>]}]}
    (the first link that works is kept), or the older {"urls": [...]}, where each link is matched
    to its lecture by the content id in its path. Queued PDFs waiting for
    a link start (or resume) with it; nothing new is queued here.
    """
    if request.method == "OPTIONS":
        resp = HttpResponse()
    elif request.method == "POST":
        by_content = {}
        for lec in Lecture.objects.exclude(note_url=""):
            m = CONTENT_ID.search(lec.note_url)
            if m:
                by_content.setdefault(m[1], []).append(lec.pk)
        matched = []
        body = json.loads(request.body or "{}")
        ok = lambda u: re.match(r"https://(storage-r\d+\.udvash-unmesh\.com|[0-9a-f]{32}\.r2\.cloudflarestorage\.com)/", u)
        groups = [(str(g.get("content_id", "")), [str(u).split("#")[0] for u in g.get("urls", []) if ok(str(u))])
                  for g in body.get("links", [])]
        groups = [(cid, urls) for cid, urls in groups if cid and urls]
        for (cid, _), url in zip(groups, _pick_working([urls for _, urls in groups])):
            pks = by_content.get(cid, [])
            Lecture.objects.filter(pk__in=pks).update(note_url=url)
            matched += pks
        for url in body.get("urls", []):
            url = str(url).split("#")[0]
            m = CONTENT_ID.search(url)
            # Notes live on Udvash's own storage or on their Cloudflare R2 bucket.
            if m and re.match(r"https://(storage-r\d+\.udvash-unmesh\.com|[0-9a-f]{32}\.r2\.cloudflarestorage\.com)/", url):
                pks = by_content.get(m[1], [])
                Lecture.objects.filter(pk__in=pks).update(note_url=url)
                matched += pks
        downloader.links_updated(matched)
        resp = JsonResponse({"matched": len(matched)})
    else:
        resp = HttpResponse(status=405)
    return _portal_cors(resp)


@require_POST
def api_pdfs(request):
    added = downloader.start_pdf_job(_ids(request))
    return JsonResponse({"added": added, **downloader.pdf_status()})


@require_POST
def api_course_load(request):
    """"⟳ Load course": remember the course link and have the feeder read the course from Udvash."""
    try:
        course.request_load(json.loads(request.body or "{}").get("url", ""))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(course.status())


def api_course(request):
    return JsonResponse({**course.status(), "lectures": course.lectures().count()})


@csrf_exempt
def api_course_feed(request):
    """For the feeder: GET whether to read the course now; POST a page of cards it read."""
    if request.method == "OPTIONS":
        return _portal_cors(HttpResponse())
    # A course load keeps the feeder busy for minutes: count it as checking in, or the extension
    # takes the quiet tab for dead and replaces it mid-load.
    downloader.feeder["last_seen"] = time.time()
    if request.method == "POST":
        course.receive(json.loads(request.body or "{}"))
        return _portal_cors(JsonResponse({"ok": True}))
    return _portal_cors(JsonResponse(course.job()))


def api_status(request):
    downloader.ensure_started()
    here = course.lectures()  # the current course
    rows = here.exclude(status="").values("id", "title", "status", "progress", "speed", "error")
    with_note = here.exclude(note_url="")
    return JsonResponse({
        "lectures": {r["id"]: r for r in rows},
        "pdf": downloader.pdf_status(),
        "pdf_local": [lec.pk for lec in with_note if lec.pdf_saved],  # notes saved on this PC
        "pdf_total": with_note.count(),
        "video_total": here.exclude(youtube_id="", portal_url="", direct_url="").count(),
        # Saved on this PC, where the app can play it.
        "video_done": sum(lec.video_saved for lec in here.exclude(status="")),
        "course_id": course.current(),
        "feeder_ago": round(time.time() - downloader.feeder["last_seen"]) if downloader.feeder["last_seen"] else None,
        "course": course.status(),
    })
