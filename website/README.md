# AEF-GRiTS installation website

Buildless, Chinese-first landing page for the **locally running Python downloader**.
This directory is not the downloader UI and has no application server, login,
upload endpoint, analytics, external fonts or JavaScript dependencies.
JavaScript shows a desktop-use notice on phones and copies commands or the page
URL to the clipboard. If clipboard access is unavailable, the text remains
available for manual copying. It never runs commands, calls a local API or probes
the visitor's ports.

`index.html` owns the content and existing clipboard behavior. `home.css` owns
the homepage layout; `tokens.css` and `base.css` are its local Dose theme starter
sources. Keep these four files together when publishing the static directory.
The homepage uses local Chinese/system fonts and does not import workbench CSS.

## Preview

Open `index.html` directly, or serve only this directory:

```bash
python -m http.server 8080 --bind 127.0.0.1 --directory website
```

The command assumes the repository root is the current directory. It does not
start the downloader. Run `aef-grits-web` separately to start the local GUI.
The page's localhost link cannot install or launch Python and does not probe
the visitor's ports.

## First-use page

Phones show a short “请在电脑上打开” notice and a copy-URL button instead of the
installation and download instructions. Phone detection uses the browser's device
identifier, not window width: narrowing a desktop window keeps the full page.
This is a convenience notice, not an access restriction; requesting a desktop
site or disabling JavaScript can show the full guide.

Link to `index.html#install` for the guided setup:

1. Install Python and obtain a source archive matching the documented GUI.
2. Copy the Windows or macOS/Linux installation commands into a local terminal.
   Installation is one-time; reuse only the launch command on later visits.
3. The launcher opens the browser. The page also links to
   `http://127.0.0.1:5555/portal` in a new tab for an **already running** workbench.
   If another port was selected, use the URL printed by the launcher instead.

This static site retains a visible development-release notice until the current
GUI has a tested, published installation package. The Releases link is an index,
not a claim that the current local GUI is already available there.

Browser regression coverage is in `tests/test_website_first_run.py`; it checks
real clipboard copying, fallback selection, no unsolicited network requests,
phone notices, narrow desktop layouts and operation without JavaScript. This is not a fresh-machine
Python/dependency installation or an OAuth consent test.
`tests/test_website_layout.py` additionally checks the 320/390/1280/1440/1920px
layouts, first-screen actions, capped desktop width and keyboard entry. It also
checks the compact privacy/footer area and keyboard expansion without clipping.

## Before public deployment

- Publish and verify a source archive or wheel containing the documented Web
  launcher, then replace the development-release notice with the actual release
  link. Do not claim a PyPI version exists before publishing it.
- Keep GCS anonymous extraction marked experimental until the complete worker
  and inventory path passes acceptance. Network reachability alone is not enough.
- Distinguish Zarr + GeoTIFF from GeoTIFF-only delivery. The latter removes only
  its task-owned temporary Zarr after validation; failure retains it for resume.
  Both still need cache plus TIFF peak space. Existing Zarr products are not deleted.
- The native folder chooser runs on the Python host and needs optional Tk plus
  a desktop session. Keep the confined Web folder-browser fallback visible;
  never imply that a remote server can open the visitor's native file dialog.
- Review Windows/macOS/Linux installation instructions against actual release
  tests. Source-relative installation uses `pip install ".[download,web]"` and
  needs the user to first extract the matching source archive.
- Review links and privacy/license text. Shared screenshots, if added, must not
  expose credentials, real Project IDs or private paths.

No hosting service was registered, no domain was configured, and no deployment
was performed. The source may later be hosted as static files after explicit
approval; it must never be used to expose the local downloader's API publicly.
