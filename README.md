# Kitsu Iko

Desktop tool for bulk upload and download against a self-hosted Kitsu (CGWire)
server. Built with Python, PyQt6 and [gazu](https://github.com/cgwire/gazu).

## Disclaimer
This app was made with Claude Code. It is open for anyone to use or tweak and will remain this way. 

## Run

```
run.bat              # first run creates .venv and installs requirements
run.bat --demo       # try it against an in-memory fake Kitsu (no server needed)
```

or manually: `.venv\Scripts\python -m kitsu_iko [--demo]`

Sign in with the URL you use in the browser (`/api` is appended for you;
`https://` is assumed, so type `http://` explicitly for a plain-http LAN server).
Untick **Verify SSL** if the server uses a self-signed certificate. "Remember
me" stores the password in Windows Credential Manager, never in a file.

## Upload

1. Pick **Project → Episode → Shots/Assets → Task**.
2. Drag files or whole folders onto the window (or *Add files… / Add folder…*).
3. Each file is auto-matched to a shot/asset by name:
   * `EP01_SQ010_SH0020_anim_v003.mov`, `sq10-sh20.mp4` and `SQ010SH020.mov`
     all match `SQ010 / SH020`. Zero-padding and separators don't matter.
   * When a shot name exists in several sequences and the filename has no
     sequence, or nothing matches at all, the file goes to the **Unassigned
     pool** on the right. Suggested matches are starred (★).
4. Fix mappings:
   * Pool: select files, pick a shot/asset in the list, then *Assign* (or double-click it).
   * Table: double-click the *Shot / Asset* cell, or right-click and choose
     *Assign to…* or *Move to unassigned pool*.
5. Set the **defaults** in the top bar: status, revision vs attachment,
   comment. Every file uses them unless overridden. To override, double-click a
   cell or select rows, right-click, and choose *Set status / Upload as / Set
   comment*. Overrides show in **bold**; inherited defaults show in grey
   italics. *Reset to defaults* removes overrides.
6. **Review & upload** shows exactly which comments get posted, plus warnings, before anything is sent.

Comment placeholders: `{filename}`, `{version}` (the highest `v###` among the files in that comment), and `{entity}`.

Options:
* **One comment per shot/asset**: files bound for the same task (with the
  same status and comment) are posted as one comment. Several revision files
  become one revision with multiple previews.
* **Revision number from filename**: uses `v###` from the filename instead of Kitsu's auto-increment.
* **Create missing tasks** (supervisor/manager/admin only): without this
  option, files whose shot has no task of the chosen type are blocked (red).
* **Set as thumbnail**.

**Upload mode** is the blue **Revision** / amber **Attachment** toggle. It
always starts on Revision when the app opens. Each file's "Upload as" cell is
tinted the same colour, the Upload button shows the counts (e.g. "Upload 5
revisions + 2 attachments"), and the review screen warns in amber when
anything is going up as an attachment. It calls out video and image files
specifically.

**Undo upload…** lists past uploads (kept in
`%LOCALAPPDATA%\KitsuIko\upload_history.json`, so it survives restarts).
Undoing one deletes the comments it posted; Kitsu removes their revisions and
attachments with them. Each task's status is put back to what it was just
before the upload, and the thumbnail too, if the upload set it. Before
deleting, the tool checks Kitsu again:
* If someone replied to one of those comments, the confirmation tells you,
  because the replies are deleted too.
* If someone commented on a task after the upload, the comment is still
  removed, but that task's status is left alone.
* Tasks created by "Create missing tasks" are kept.

After an undo, the affected files return to *Ready* in the list, so you can
fix them and upload again.

Failures are per comment. If a comment was posted but one of its files failed,
**Retry** adds the missing files to *that same comment*, so nothing is
double-posted. Status choices are limited to what your Kitsu role may set.

## Download

1. Pick the same context, then choose **Revisions** or **Comment attachments**.
2. Every shot/asset lists its **latest** version, preselected. To change one,
   double-click *Version* and pick another or *(skip this one)*. To change
   several, right-click and choose *Use previous* or *Use revision number…*.
   You can also untick rows, filter by name or by task status (e.g. only
   `WFA`), and use *Reset to latest*.
3. Choose the destination folder, a sub-folder layout, and a file-name
   template. Tokens: `{project} {episode} {sequence} {entity} {entity_full}
   {asset_type} {task} {rev} {status} {original}`. A live example path is
   shown below the templates.
4. **Download**. Existing files can be skipped. Clicking Download again only
   retries rows that haven't finished.

Note: for movies, Kitsu serves its normalized `.mp4` rather than the
originally uploaded file. Pictures and other files come back as uploaded.

## Build the .exe

```
build.bat            # or: .venv\Scripts\python build.py
```

This produces `dist\KitsuIko.exe`, a single 42 MB file that runs without
Python, with the icon and version 0.9 in its file properties. The build
launches the new exe with a self-test (Qt, icon, Windows Credential Manager,
HTTPS certificates, UI) and reports **PROBLEM** if anything is missing. To
release a new version, change `kitsu_iko/__init__.py` and rebuild.

Sharing it:
* Just copy `KitsuIko.exe` anywhere and double-click it. Each person signs in
  with their own Kitsu account. Settings and the saved password are stored
  per Windows user, not next to the exe.
* The exe isn't code-signed, so on other PCs Windows SmartScreen may say
  "Windows protected your PC": click **More info → Run anyway**. Some
  antivirus tools are wary of unsigned PyInstaller apps. A code-signing
  certificate removes both problems.
* Startup takes a second or two, because the exe unpacks itself to a
  temporary folder each time it starts.

## Development

```
.venv\Scripts\python -m unittest discover -s tests -t .
```

| File | Role |
|---|---|
| `kitsu_iko/matcher.py` | filename → shot/asset matching (pure, tested) |
| `kitsu_iko/models.py` | row data + upload planner (grouping, warnings, blockers; tested) |
| `kitsu_iko/service.py` | every gazu/network call; one client per thread |
| `kitsu_iko/history.py`, `undo_dialog.py` | upload journal and undo (logic tested) |
| `kitsu_iko/demo_service.py` | in-memory fake server for `--demo` |
| `kitsu_iko/upload_tab.py`, `download_tab.py` | the two tabs |
| `kitsu_iko/widgets.py`, `login.py`, `workers.py`, `app.py` | shared UI, sign-in, threading, main window |
| `build.py`, `build.bat`, `kitsu_iko_main.py` | packaging into `dist\KitsuIko.exe` |

Uploads run 3 at a time (`workers.TRANSFER_POOL`) because each preview makes
the server transcode. gazu sends each upload in a single request, so the
progress bar reaches the end before the server has finished ("finishing..."),
and each running upload holds its file in memory. Keep that in mind for
multi-GB files.
