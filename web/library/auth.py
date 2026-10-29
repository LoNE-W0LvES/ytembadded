"""Optional password for devices on the home network (settings.APP_PASSWORD)."""
import hmac

from django.conf import settings
from django.shortcuts import redirect, render

# The Udvash tab's link feeder calls these from another site, without our login cookie.
OPEN_PATHS = ("/login/", "/api/note-links/", "/api/video-links/", "/api/feeder/wanted/", "/api/course/feed/")
LOOPBACK = ("127.0.0.1", "::1")


class PasswordMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (not settings.APP_PASSWORD or request.META.get("REMOTE_ADDR") in LOOPBACK
                or request.path.startswith(OPEN_PATHS) or request.session.get("ok")):
            return self.get_response(request)
        if request.path.startswith(("/api/", "/file/")):
            from django.http import JsonResponse
            return JsonResponse({"error": "login required"}, status=401)
        return redirect(f"/login/?next={request.get_full_path()}")


def login(request):
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.POST.get("password", ""), settings.APP_PASSWORD):
            request.session["ok"] = True
            nxt = request.POST.get("next") or "/"
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        error = "Wrong password."
    return render(request, "library/login.html", {"error": error, "next": request.GET.get("next", "/")})
