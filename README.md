# Course Library

A small Django app for watching and downloading Udvash-Unmesh course lectures and notes from any
device on your home network, plus a Chrome extension (the "link feeder") that fetches fresh
download links from your logged-in Udvash account.

## Run

```powershell
.\run_library.ps1            # home network (phones, other PCs)
.\run_library.ps1 -Local     # this PC only
```

The first run creates `.venv`, installs `requirements.txt` and sets up the database. Later runs
just start the app. If Windows blocks the script:
`powershell -ExecutionPolicy Bypass -File .\run_library.ps1`

Put a password in `app_password.txt` (next to the script) to require a login from other devices.

## Link feeder extension

`run_library.ps1` sets it up too. On the first run (and whenever the extension changes) it copies
it to `%LOCALAPPDATA%\CourseLibrary\feeder-extension`, puts that path on the clipboard and opens
`chrome://extensions`. The first time, turn on **Developer mode**, click **Load unpacked** and paste
the path; after an update, click the reload arrow on the extension. (Chrome only installs
extensions from outside the Web Store this way.) Chrome on that PC must be logged in to Udvash.

It opens a pinned Udvash tab by itself whenever the app needs links, and closes it when idle.

## Load a course

On the main page, paste a link from any page of the course on Udvash and press **⟳ Load course**.
The extension reads the course's subjects, chapters and lecture cards and sends them to the app.
