import re

from django.conf import settings
from django.db import models


def clean(name, limit=None):
    """Make text safe for a Windows file or folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") if limit else name


class Lecture(models.Model):
    STATUS_CHOICES = [
        ("", "Not downloaded"),
        ("queued", "Queued"),
        ("downloading", "Downloading"),
        ("done", "Downloaded"),
        ("failed", "Failed"),
    ]

    key = models.CharField(max_length=400, unique=True)  # YouTube id, or subject|title
    order = models.PositiveIntegerField(db_index=True)
    subject = models.CharField(max_length=200)
    chapter = models.CharField(max_length=300)
    class_type = models.CharField(max_length=100, blank=True)
    title = models.TextField()
    youtube_id = models.CharField(max_length=20, blank=True)
    direct_url = models.TextField(blank=True)  # for lectures that are only on the portal
    portal_url = models.TextField(blank=True)
    note_url = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, blank=True, default="")
    progress = models.FloatField(default=0)
    speed = models.CharField(max_length=40, blank=True)
    error = models.TextField(blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    # Udvash's id for the lecture's card (masterContentId); "Load course" matches lectures by it.
    content_id = models.CharField(max_length=20, blank=True, db_index=True)
    # Where it appears in the course. Separate from `order`, which is part of the file names
    # (base_path) and so never changes once a lecture exists.
    position = models.PositiveIntegerField(default=0, db_index=True)
    # Udvash course it was last loaded from (masterCourseId); the main page shows the current course.
    course = models.CharField(max_length=20, blank=True, db_index=True)

    class Meta:
        ordering = ["position", "order"]

    def __str__(self):
        return self.title

    @property
    def base_path(self):
        # Same layout as download.py: downloads/<Subject>/<Chapter>/<NNN> <Lecture>
        return (settings.DOWNLOAD_DIR / clean(self.subject) / clean(self.chapter)
                / clean(f"{self.order:03d} {self.title}", 90))

    # Append (not with_suffix): titles like "H.Math" contain dots.
    @property
    def video_path(self):
        return self.base_path.with_name(self.base_path.name + ".mp4")

    @property
    def pdf_path(self):
        return self.base_path.with_name(self.base_path.name + ".pdf")

    @property
    def can_download(self):
        return bool(self.youtube_id or self.direct_url)

    @property
    def pdf_saved(self):
        """The note is saved on this PC."""
        return self.pdf_path.exists()

    @property
    def video_saved(self):
        return self.video_path.exists()

    has_pdf = pdf_saved
