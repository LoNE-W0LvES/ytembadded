import os
import sys

from django.apps import AppConfig


class LibraryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'library'

    def ready(self):
        # COURSEHUB_MDNS=<name> (set by run_library.ps1 in network mode): announce <name>.local.
        # runserver loads the app twice (a file watcher plus the server); only the server announces.
        name = os.environ.get("COURSEHUB_MDNS", "").strip()
        if not name or "runserver" not in sys.argv or os.environ.get("RUN_MAIN") != "true":
            return
        addr = next((a for a in sys.argv[sys.argv.index("runserver") + 1:] if not a.startswith("-")), "8000")
        from . import mdns
        url = mdns.announce(name, int(addr.rsplit(":", 1)[-1]))
        if url:
            print(f"Also at {url} (any device on this Wi-Fi)", flush=True)
