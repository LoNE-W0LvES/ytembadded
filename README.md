# Course Library

A small Django app for watching and downloading Udvash-Unmesh course lectures and notes from any
device on your home network, plus a Chrome extension (the "link feeder") that fetches fresh
download links from your logged-in Udvash account.

## Run

```powershell
.\run_library.ps1            # home network: open http://courses.local on phones and other PCs
.\run_library.ps1 -Local     # this PC only (http://localhost)
.\run_library.ps1 -Name lib  # announce http://lib.local instead
```

On the home network the app announces itself over mDNS, like a microcontroller does, so other
devices don't need the PC's IP address. It uses port 80 so the address has no `:port`; if another
program already has port 80 it falls back to 8000 (`http://courses.local:8000`). Most phones and
PCs open `.local` names; if one doesn't, use the IP address the script prints.

The first run creates `.venv`, installs `requirements.txt`, installs FFmpeg and Node.js with winget
if they're missing (needed for 720p YouTube downloads), installs the browser the app logs in to
Udvash with and sets up the database. Later runs just
start the app. You need Python 3.10+. If Windows blocks the script:
`powershell -ExecutionPolicy Bypass -File .\run_library.ps1`

Put a password in `app_password.txt` (next to the script) to require a login from other devices.

## Udvash login

On the main page, enter your Udvash registration number (or mobile number) and password once and
press **Save login**. The app keeps them on the PC it runs on (`web/udvash_login.json`, not in git)
and never shows the password again. Whenever it needs something from Udvash (a course load, fresh
note or video links) it logs in with a hidden browser (Playwright's Chromium, which
`run_library.ps1` installs) and closes it again when idle. No Chrome or extension needed on any PC.

If Udvash rejects the login, the app shows Udvash's message and doesn't retry until you save the
login again, so a typo can't lock the account. Logging in from the app may sign out your other
Udvash sessions.

## Load a course

On the main page, paste a link from any page of the course on Udvash and press **⟳ Load course**.
The app reads the course's subjects, chapters and lecture cards from Udvash.

## Chrome extension (optional)

`feeder-extension/` is the older way to reach Udvash: a Chrome extension that does the same work in
a logged-in Chrome tab. The app's hidden browser runs the same `feeder.js`. Without a saved login
the app still accepts links from the extension (install it with **Load unpacked** on
`chrome://extensions`); with one, the extension stays idle.
