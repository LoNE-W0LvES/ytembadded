from .models import Lecture


def sync_files():
    """Mark lectures whose video is already on disk as downloaded."""
    for lec in Lecture.objects.exclude(status__in=["done", "queued", "downloading"]):
        if lec.video_saved:
            Lecture.objects.filter(pk=lec.pk).update(status="done", progress=100, error="")
