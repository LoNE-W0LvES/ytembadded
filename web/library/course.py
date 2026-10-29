"""Loading the lecture list straight from Udvash ("Load course" on the main page), instead of a CSV.

The app can't read Udvash itself (it needs your logged-in session), so the link feeder extension
does it: the app asks for a load (job), the feeder walks the course's subjects -> chapters -> class
types -> cards in the logged-in tab and sends back what each card says (receive), a page of cards
at a time.

Lectures already here are matched by YouTube id or Udvash content id and only get their position
and any missing links updated. Their subject/chapter/title/order stay as they are, because those
make up the names of the files already saved on this PC.
"""
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from .models import Lecture

COURSE_FILE = settings.BASE_DIR / "course.json"
PORTAL = "https://online.udvash-unmesh.com"
_lock = threading.Lock()
state = {"pending": False, "running": False, "asked": 0.0, "heard": 0.0, "cards": 0, "added": 0,
         "updated": 0, "done_types": 0, "total_types": 0, "finished": 0.0, "error": ""}
_position = [0]  # next course position during a load


def _saved():
    try:
        return json.loads(COURSE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(**changes):
    COURSE_FILE.write_text(json.dumps({**_saved(), **changes}), encoding="utf-8")


def start_url():
    """The course's subject list on Udvash: the course last loaded, else settings.FEEDER_START_URL."""
    return _saved().get("start") or settings.FEEDER_START_URL


def current():
    """The current course's id (masterCourseId): the main page shows its lectures."""
    return (parse_qs(urlsplit(start_url()).query).get("masterCourseId") or [""])[0]


def lectures():
    return Lecture.objects.filter(course=current())


def normalize(url):
    """Any link from inside the course -> its subject list page. ValueError if it isn't Udvash."""
    u = urlsplit(url.strip())
    if u.scheme != "https" or u.hostname != "online.udvash-unmesh.com":
        raise ValueError("Paste a link from online.udvash-unmesh.com (any page of the course).")
    q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
    course = q.get("mastercourseid")
    if not course:
        raise ValueError("That link doesn't say which course it is (no masterCourseId in it). "
                         "Open the course on Udvash and copy the address of its subject list.")
    kind = q.get("coursetypeid") or q.get("mastercoursetypeid") or "2"
    return f"{PORTAL}/Content/ContentSubject?CourseTypeId={kind}&masterCourseId={course}"


def request_load(url):
    start = normalize(url)
    # On disk too: the dev server restarts whenever its code changes, and a waiting load must survive that.
    _remember(start=start, pending=True)
    with _lock:
        state.update(pending=True, running=False, asked=time.time(), heard=0.0, cards=0, added=0, updated=0,
                     done_types=0, total_types=0, finished=0.0, error="")
    return start


def wanted():
    """True while a load is waiting for the feeder or running (the extension opens the tab for it)."""
    with _lock:
        stale = state["running"] and time.time() - state["heard"] > 180  # the feeder tab was closed mid-load
        if stale:
            state.update(running=False, pending=True)
        return state["pending"] or state["running"]


def job():
    """For the feeder: a load to run now, with the content ids the app already has (those cards
    don't need their video page read again)."""
    with _lock:
        if not state["pending"]:
            return {"load": False}
        state.update(pending=False, running=True, heard=time.time())
    _position[0] = 0
    # YouTube ids don't change, so those lectures are settled. Portal-only ones are read again in
    # case Udvash has put them on YouTube since.
    known = list(Lecture.objects.exclude(content_id="").exclude(youtube_id="").values_list("content_id", flat=True))
    return {"load": True, "start": start_url(), "known": known}


def receive(data):
    """A page of cards from the feeder: {"lectures": [...], "done_types", "total_types", "done", "error"}."""
    items = data.get("lectures") or []
    with transaction.atomic():
        for item in items:
            _save(item)
    with _lock:
        state.update(heard=time.time(), cards=state["cards"] + len(items),
                     done_types=int(data.get("done_types") or state["done_types"]),
                     total_types=int(data.get("total_types") or state["total_types"]))
        if data.get("error"):
            state.update(running=False, error=str(data["error"])[:300], finished=time.time())
        elif data.get("done"):
            state.update(running=False, finished=time.time())
    if data.get("error") or data.get("done"):
        _remember(pending=False)
    if data.get("done"):
        from .importer import sync_files
        sync_files()


def _save(item):
    content_id = str(item.get("content_id") or "").strip()
    youtube_id = str(item.get("youtube_id") or "").strip()
    if not content_id:
        return
    _position[0] += 1
    # Content id first: it's what Udvash itself uses for the card. A YouTube id only finds lectures
    # from before content ids were recorded.
    lec = Lecture.objects.filter(content_id=content_id).first() \
        or (Lecture.objects.filter(youtube_id=youtube_id).first() if youtube_id else None)
    note_url = str(item.get("note_url") or "").split("#")[0]
    portal_url = "" if youtube_id else str(item.get("video_card") or "")
    if portal_url.startswith("/"):
        portal_url = PORTAL + portal_url
    if lec:
        changes = {"position": _position[0], "content_id": content_id, "course": current()}
        if youtube_id and not lec.youtube_id:
            changes["youtube_id"] = youtube_id
        if portal_url and not lec.portal_url and not lec.youtube_id:
            changes["portal_url"] = portal_url
        if note_url:
            changes["note_url"] = note_url
        Lecture.objects.filter(pk=lec.pk).update(**changes)
        with _lock:
            state["updated"] += 1
        return
    order = (Lecture.objects.aggregate(m=Max("order"))["m"] or 0) + 1  # a new, unused file number
    Lecture.objects.create(
        key=youtube_id or f"udvash:{content_id}", order=order, position=_position[0],
        subject=str(item.get("subject") or "").strip()[:200],
        chapter=str(item.get("chapter") or "").strip()[:300],
        class_type=str(item.get("class_type") or "").strip()[:100],
        title=str(item.get("title") or "").strip() or f"Lecture {content_id}",
        youtube_id=youtube_id, portal_url=portal_url, note_url=note_url, content_id=content_id, course=current())
    with _lock:
        state["added"] += 1


# A load asked for before a restart is still waiting.
if _saved().get("pending"):
    state["pending"] = True


def status():
    with _lock:
        return {**state, "start": start_url()}
