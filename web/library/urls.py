from django.urls import path

from . import auth, views

urlpatterns = [
    path("", views.index, name="index"),
    path("login/", auth.login, name="login"),
    path("api/course/", views.api_course, name="api_course"),
    path("api/course/load/", views.api_course_load, name="api_course_load"),
    path("api/udvash/", views.api_udvash, name="api_udvash"),
    path("api/course/feed/", views.api_course_feed, name="api_course_feed"),
    path("api/downloads/action/", views.api_downloads_action, name="api_downloads_action"),
    path("api/limits/", views.api_limits, name="api_limits"),
    path("watch/<int:pk>/", views.watch, name="watch"),
    path("watch/<int:pk>/direct-link/", views.set_direct_url, name="set_direct_url"),
    path("file/<int:pk>/<str:kind>/", views.serve_file, name="file"),
    path("note/<int:pk>/", views.note, name="note"),
    path("get/", views.get_bulk, name="get_bulk"),
    path("get/<int:pk>/video360/file/", views.get_360, name="get_360"),
    path("get/<int:pk>/video/file/", views.get_file, name="get_file"),
    path("get/<int:pk>/<str:kind>/relay/", views.get_relay, name="get_relay"),
    path("get/<int:pk>/<str:kind>/", views.get_page, name="get"),
    path("api/get/<int:pk>/<str:kind>/", views.api_get, name="api_get"),
    path("api/note/<int:pk>/", views.api_note_state, name="api_note_state"),
    path("api/pdfs/", views.api_pdfs, name="api_pdfs"),
    path("api/note-links/", views.api_note_links, name="api_note_links"),
    path("api/note-links/needed/", views.api_links_needed, name="api_links_needed"),
    path("api/video-links/", views.api_video_links, name="api_video_links"),
    path("api/feeder/wanted/", views.api_feeder_wanted, name="api_feeder_wanted"),
    path("api/queue/", views.api_queue, name="api_queue"),
    path("api/cancel/", views.api_cancel, name="api_cancel"),
    path("api/status/", views.api_status, name="api_status"),
]
