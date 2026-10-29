"""Settings for the Udvash course library (local, single-user app)."""
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# A random key per install (it signs the login cookie), kept next to the database.
_key_file = BASE_DIR / "secret_key.txt"
if not _key_file.exists():
    _key_file.write_text(secrets.token_urlsafe(50))
SECRET_KEY = _key_file.read_text().strip()
DEBUG = True
ALLOWED_HOSTS = ["*"]  # reachable by this PC's address on the home network (run_library.ps1)

# Password for other devices on the network (this PC itself never needs it).
# Put it in ytembadded/app_password.txt; no file = no password.
_pw_file = BASE_DIR.parent / "app_password.txt"
APP_PASSWORD = _pw_file.read_text(encoding="utf-8").strip() if _pw_file.exists() else ""
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30

INSTALLED_APPS = ["library"]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "library.auth.PasswordMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "coursehub.urls"
WSGI_APPLICATION = "coursehub.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 30},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "Asia/Dhaka"

# Downloads
DOWNLOAD_DIR = BASE_DIR.parent / "downloads"  # same folder download.py uses
DOWNLOAD_QUALITY = 720  # max video height
DOWNLOAD_WORKERS = 3  # videos downloaded at once
FFMPEG_DIR = Path.home() / "AppData/Local/Microsoft/WinGet/Links"

# The course's subject list on Udvash, until a course is loaded from the main page (course.json).
FEEDER_START_URL = ""
