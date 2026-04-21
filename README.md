# KiwiEater

A Linux-only website archiver designed for forum sites (kiwifarms.net and
similar). Produces a self-contained, fully offline-browsable copy of a site:
an `index.html` at the root, mirrored sub-pages, and assets (images, CSS, JS,
fonts) stored under `_assets/`. All in-scope links are rewritten to relative
local paths so the archive works with a plain `file://` open.

## Features

- BFS crawl from a seed URL; stays within the seed host (plus any explicit
  extra hosts you allow).
- Rewrites `<a>`, `<img>`, `<link>`, `<script>`, `<source>`/`srcset`,
  `<iframe>`, `<video>`, `<audio>`, inline `style=""`, `<style>` blocks, and
  `url(...)` / `@import` in downloaded CSS.
- Images resized (longest side) and recompressed via Pillow. Opaque images go
  to optimized JPEG; images with transparency stay as optimized PNG; animated
  GIFs are preserved frame-accurate.
- Resume on restart: every N downloads and on Ctrl+C / SIGTERM, state is
  written to `_state.json`. Re-run the command (or just `-o <dir>` with no
  URL) to continue.
- Authenticated forums: pass a Netscape `cookies.txt` via `--cookies`.
- Polite by default (0.5s delay between requests, configurable).

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ recommended.

## Usage

Start a new archive:

```bash
python3 kiwieater.py https://example-forum.net/ -o backup_output
```

Resume (state file in the output dir is all that's needed):

```bash
python3 kiwieater.py -o backup_output
```

Authenticated:

```bash
python3 kiwieater.py https://forum.tld/ -o out --cookies ./cookies.txt
```

Common flags:

| flag | default | meaning |
| --- | --- | --- |
| `--max-depth` | 5 | BFS depth from the seed (`-1` = unlimited) |
| `--max-pages` | 0 | Stop after N pages (0 = unlimited) |
| `--delay` | 0.5 | Seconds between requests |
| `--timeout` | 30 | Per-request timeout |
| `--image-max-dim` | 1280 | Resize images so longest side <= this |
| `--image-quality` | 72 | JPEG recompression quality |
| `--no-compress` | off | Skip image recompression entirely |
| `--allowed-hosts` | seed only | Extra hostnames to crawl into |
| `--cookies` | — | Netscape cookies.txt for auth |
| `--user-agent` | KiwiEater/1.0 | Override UA |
| `--save-every` | 10 | Flush state every N items |
| `-v` | off | Debug logging |

## Output layout

```
backup_output/
├── index.html              # seed URL lives here
├── _state.json             # resume state
├── _manifest.json          # URL -> local path index
├── _backup.log             # full log (debug level)
├── <host>/<mirrored-path>/page.html
└── _assets/
    ├── img/  (hashed filenames, compressed)
    ├── css/  (url(...) rewritten to local)
    ├── js/
    ├── font/
    └── other/
```

Open `backup_output/index.html` in any browser — no server required.

## Limitations

- Content loaded dynamically via JavaScript / XHR / infinite scroll will not
  be captured (this is a static archiver).
- `<form>` actions are left as absolute URLs — they don't function offline.
- `robots.txt` is not consulted; be a polite citizen and use `--delay`.
- Only you are responsible for ensuring your archival is lawful and permitted.
