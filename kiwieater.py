#!/usr/bin/env python3
"""
KiwiEater - forum-site backupper / offline archiver.

Creates a fully navigable offline copy of a website starting from a seed URL.
Produces an index.html at the output root, mirrors the site's URL structure for
pages, stores assets under _assets/, rewrites all in-scope links to local
relative paths, compresses/resizes images to save disk space, and persists
progress to _state.json so an interrupted run resumes on restart.

Linux-only. Uses only real, actively maintained libraries:
    requests, beautifulsoup4, lxml, Pillow, cssutils, tqdm
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse

import cssutils
import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
from tqdm import tqdm


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

STATE_FILE = "_state.json"
MANIFEST_FILE = "_manifest.json"
LOG_FILE = "_backup.log"

ASSET_DIRS = {
    "image": "_assets/img",
    "css": "_assets/css",
    "js": "_assets/js",
    "font": "_assets/font",
    "other": "_assets/other",
}

# Attributes that carry URLs in HTML, keyed by tag name.
URL_ATTRS: dict[str, tuple[str, ...]] = {
    "a": ("href",),
    "link": ("href",),
    "img": ("src", "data-src", "data-original"),
    "source": ("src", "srcset"),
    "script": ("src",),
    "iframe": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "track": ("src",),
    "form": ("action",),
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
SKIP_COMPRESS_EXTS = {".svg", ".ico", ".avif"}

# Silence noisy cssutils warnings - forum CSS is often non-compliant.
cssutils.log.setLevel(logging.CRITICAL)


# --------------------------------------------------------------------------- #
# State                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class Stats:
    pages_saved: int = 0
    assets_saved: int = 0
    images_compressed: int = 0
    bytes_downloaded: int = 0
    bytes_written: int = 0
    failed: int = 0


@dataclass
class State:
    start_url: str
    allowed_hosts: list[str]
    # Queue of [url, depth] pairs still to fetch.
    queue: list[list] = field(default_factory=list)
    # URL -> local path (relative to output root).
    visited: dict[str, str] = field(default_factory=dict)
    # URL -> error message.
    failed: dict[str, str] = field(default_factory=dict)
    stats: Stats = field(default_factory=Stats)

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, data: dict) -> "State":
        stats_data = data.pop("stats", {})
        s = cls(**data)
        s.stats = Stats(**stats_data)
        return s


# --------------------------------------------------------------------------- #
# URL helpers                                                                 #
# --------------------------------------------------------------------------- #

def normalize_url(url: str, base: Optional[str] = None) -> str:
    """Resolve against base, strip fragment, normalize."""
    if base:
        url = urljoin(base, url)
    url, _frag = urldefrag(url)
    parsed = urlparse(url)
    # Lowercase scheme/host, strip default ports.
    netloc = parsed.hostname or ""
    if parsed.port and not (
        (parsed.scheme == "http" and parsed.port == 80)
        or (parsed.scheme == "https" and parsed.port == 443)
    ):
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    return urlunparse(
        (parsed.scheme.lower(), netloc, path, parsed.params, parsed.query, "")
    )


def is_in_scope(url: str, allowed_hosts: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_segment(seg: str, max_len: int = 120) -> str:
    seg = _SAFE_SEGMENT_RE.sub("_", seg)
    seg = seg.strip("._") or "_"
    if len(seg) > max_len:
        digest = hashlib.sha1(seg.encode("utf-8")).hexdigest()[:10]
        seg = seg[:max_len - 11] + "_" + digest
    return seg


def url_to_local_path(url: str, kind: str, is_start: bool = False) -> str:
    """
    Deterministic mapping from URL to a safe relative path inside the output.
    - Start URL always maps to 'index.html'.
    - Pages mirror the URL path; query strings are appended to the filename.
    - Assets go under _assets/<subdir>/ with a content-addressed filename
      prefix to avoid collisions across hosts.
    """
    if is_start:
        return "index.html"

    parsed = urlparse(url)
    path = parsed.path or "/"

    if kind == "page":
        segments = [s for s in path.split("/") if s]
        safe_segments = [_sanitize_segment(s) for s in segments]
        if not safe_segments or path.endswith("/"):
            safe_segments.append("index")
            filename = safe_segments[-1]
            directory = safe_segments[:-1]
        else:
            filename = safe_segments[-1]
            directory = safe_segments[:-1]

        # If the final segment already has a recognisable extension, strip it
        # before appending '.html' so the browser serves it correctly offline.
        root, ext = os.path.splitext(filename)
        if ext.lower() in {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ""}:
            filename = root or "index"

        if parsed.query:
            q_hash = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
            filename = f"{filename}__q_{q_hash}"

        # Namespace by host so cross-host crawls don't collide.
        host = _sanitize_segment(parsed.hostname or "host")
        local = os.path.join(host, *directory, filename + ".html")
        return local

    # Asset: hash-prefix + preserved basename/extension.
    subdir = ASSET_DIRS.get(kind, ASSET_DIRS["other"])
    basename = os.path.basename(path) or "file"
    basename = _sanitize_segment(basename)
    root, ext = os.path.splitext(basename)
    if not ext:
        ext = ""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    filename = f"{digest}_{root}{ext}" if root else f"{digest}{ext}"
    return os.path.join(subdir, filename)


def classify_asset(url: str, content_type: Optional[str] = None) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "image"
    if "css" in ct:
        return "css"
    if "javascript" in ct or "ecmascript" in ct:
        return "js"
    if "font" in ct or "woff" in ct:
        return "font"

    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in IMAGE_EXTS or ext in SKIP_COMPRESS_EXTS:
        return "image"
    if ext == ".css":
        return "css"
    if ext in {".js", ".mjs"}:
        return "js"
    if ext in {".woff", ".woff2", ".ttf", ".otf", ".eot"}:
        return "font"
    return "other"


def rel_link(from_local: str, to_local: str) -> str:
    """Relative URL from one local file to another, always POSIX-style."""
    from_dir = os.path.dirname(from_local)
    rel = os.path.relpath(to_local, from_dir or ".")
    return rel.replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# Image compression                                                           #
# --------------------------------------------------------------------------- #

def compress_image(data: bytes, url: str, max_dim: int, quality: int) -> tuple[bytes, str]:
    """
    Resize and recompress an image. Returns (new_bytes, new_ext).
    Falls back to original bytes if the format can't be processed.
    """
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext in SKIP_COMPRESS_EXTS:
        return data, ext

    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        return data, ext

    fmt = (img.format or "").upper()

    # Animated GIFs: preserve animation, skip resize to keep frames aligned.
    if fmt == "GIF" and getattr(img, "is_animated", False):
        out = BytesIO()
        img.save(out, format="GIF", save_all=True, optimize=True)
        return out.getvalue(), ".gif"

    w, h = img.size
    longest = max(w, h)
    if longest > max_dim:
        ratio = max_dim / float(longest)
        new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
        img = img.resize(new_size, Image.LANCZOS)

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )

    out = BytesIO()
    if has_alpha:
        # Preserve transparency as optimized PNG.
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        img.save(out, format="PNG", optimize=True)
        return out.getvalue(), ".png"

    # Everything opaque goes to JPEG for best size/quality tradeoff.
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue(), ".jpg"


# --------------------------------------------------------------------------- #
# Crawler                                                                     #
# --------------------------------------------------------------------------- #

class Backupper:
    def __init__(self, args: argparse.Namespace, logger: logging.Logger):
        self.args = args
        self.log = logger
        self.output = Path(args.output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)

        self.state_path = self.output / STATE_FILE
        self.manifest_path = self.output / MANIFEST_FILE

        self.session = self._build_session()
        self.state = self._load_or_init_state()

        # In-memory queue mirrors state.queue for fast pops.
        self.queue: deque[tuple[str, int, str]] = deque()
        for item in self.state.queue:
            # Backwards-compatible: older state stored [url, depth].
            if len(item) == 2:
                self.queue.append((item[0], item[1], "page"))
            else:
                self.queue.append((item[0], item[1], item[2]))

        self._stop = False
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ----------------------------- setup --------------------------------- #

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": self.args.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if self.args.cookies:
            jar = http.cookiejar.MozillaCookieJar(self.args.cookies)
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies = jar
        return s

    def _load_or_init_state(self) -> State:
        start = normalize_url(self.args.url)
        parsed = urlparse(start)
        host = parsed.hostname or ""
        allowed = [host] if not self.args.allowed_hosts else list(self.args.allowed_hosts)
        if host and host not in allowed:
            allowed.append(host)

        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                st = State.from_json(data)
                if st.start_url != start:
                    self.log.warning(
                        "State file start_url (%s) differs from argument (%s); "
                        "using state.",
                        st.start_url, start,
                    )
                self.log.info(
                    "Resumed from state: %d visited, %d queued, %d failed.",
                    len(st.visited), len(st.queue), len(st.failed),
                )
                return st
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                self.log.warning("Could not read state (%s); starting fresh.", e)

        st = State(start_url=start, allowed_hosts=allowed)
        st.queue.append([start, 0, "page"])
        return st

    # ----------------------------- persistence --------------------------- #

    def _save_state(self) -> None:
        self.state.queue = [list(item) for item in self.queue]
        tmp = self.state_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.state.to_json(), f, indent=2, sort_keys=True)
        tmp.replace(self.state_path)

        with self.manifest_path.open("w", encoding="utf-8") as f:
            json.dump({
                "start_url": self.state.start_url,
                "entries": self.state.visited,
            }, f, indent=2, sort_keys=True)

    def _handle_signal(self, signum, _frame):
        self.log.warning("Received signal %d, saving state and exiting.", signum)
        self._stop = True

    # ----------------------------- HTTP ---------------------------------- #

    def _fetch(self, url: str) -> Optional[requests.Response]:
        try:
            resp = self.session.get(
                url,
                timeout=self.args.timeout,
                allow_redirects=True,
                stream=False,
            )
        except requests.RequestException as e:
            self.state.failed[url] = f"network: {e}"
            self.state.stats.failed += 1
            self.log.warning("Fetch failed %s: %s", url, e)
            return None

        if resp.status_code >= 400:
            self.state.failed[url] = f"http {resp.status_code}"
            self.state.stats.failed += 1
            self.log.warning("HTTP %d for %s", resp.status_code, url)
            return None

        self.state.stats.bytes_downloaded += len(resp.content)
        return resp

    # ----------------------------- main loop ----------------------------- #

    def run(self) -> None:
        self.log.info("Start URL: %s", self.state.start_url)
        self.log.info("Allowed hosts: %s", ", ".join(self.state.allowed_hosts))
        self.log.info("Output: %s", self.output)

        total_cap = self.args.max_pages if self.args.max_pages > 0 else None

        pbar = tqdm(
            total=total_cap,
            initial=self.state.stats.pages_saved,
            desc="pages",
            unit="pg",
            dynamic_ncols=True,
        )

        try:
            while self.queue and not self._stop:
                if total_cap and self.state.stats.pages_saved >= total_cap:
                    self.log.info("Reached max-pages limit.")
                    break

                url, depth, kind = self.queue.popleft()
                if url in self.state.visited:
                    continue
                if not is_in_scope(url, self.state.allowed_hosts):
                    continue

                if kind == "page":
                    self._process_page(url, depth)
                    pbar.update(1)
                else:
                    self._process_asset(url, kind)

                if (self.state.stats.pages_saved + self.state.stats.assets_saved) \
                        % max(1, self.args.save_every) == 0:
                    self._save_state()

                if self.args.delay > 0:
                    time.sleep(self.args.delay)
        finally:
            pbar.close()
            self._save_state()
            self._log_summary()

    # ----------------------------- page ---------------------------------- #

    def _process_page(self, url: str, depth: int) -> None:
        resp = self._fetch(url)
        if resp is None:
            return

        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        # If it's not HTML, treat it as an asset instead.
        if ctype and "html" not in ctype and "xml" not in ctype:
            kind = classify_asset(url, ctype)
            self._save_asset_bytes(url, resp.content, kind, ctype)
            return

        is_start = url == self.state.start_url
        local = url_to_local_path(url, "page", is_start=is_start)
        self.state.visited[url] = local

        try:
            html = resp.content.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = resp.content.decode("utf-8", errors="replace")

        soup = BeautifulSoup(html, "lxml")
        self._rewrite_and_enqueue(soup, url, local, depth)

        out_path = self.output / local
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = str(soup).encode("utf-8")
        out_path.write_bytes(rendered)

        self.state.stats.pages_saved += 1
        self.state.stats.bytes_written += len(rendered)
        self.log.info("page [%d] %s -> %s", depth, url, local)

    # ----------------------------- rewriting ----------------------------- #

    def _rewrite_and_enqueue(
        self,
        soup: BeautifulSoup,
        page_url: str,
        page_local: str,
        depth: int,
    ) -> None:
        # <base href="..."> confuses relative resolution; drop it but use it.
        base_tag = soup.find("base")
        base_url = page_url
        if base_tag and base_tag.get("href"):
            base_url = normalize_url(base_tag["href"], page_url)
            base_tag.decompose()

        for tag in soup.find_all(True):
            name = tag.name.lower()
            attrs = URL_ATTRS.get(name)
            if not attrs:
                continue

            for attr in attrs:
                val = tag.get(attr)
                if not val:
                    continue

                if attr == "srcset":
                    tag[attr] = self._rewrite_srcset(val, base_url, page_local, depth)
                    continue

                new_val = self._rewrite_single_url(
                    val, base_url, page_local, tag_name=name, attr=attr, depth=depth
                )
                if new_val is not None:
                    tag[attr] = new_val

            # Inline style attributes.
            style = tag.get("style")
            if style:
                tag["style"] = self._rewrite_css_text(style, base_url, page_local)

        # <style> blocks.
        for style in soup.find_all("style"):
            if style.string:
                style.string.replace_with(
                    self._rewrite_css_text(style.string, base_url, page_local)
                )

        # Mark the archive for the reader.
        if soup.head:
            meta = soup.new_tag(
                "meta",
                attrs={
                    "name": "generator",
                    "content": f"KiwiEater archive of {page_url}",
                },
            )
            soup.head.insert(0, meta)

    def _rewrite_single_url(
        self,
        raw: str,
        base_url: str,
        page_local: str,
        tag_name: str,
        attr: str,
        depth: int,
    ) -> Optional[str]:
        raw = raw.strip()
        if not raw:
            return None
        low = raw.lower()
        if low.startswith(("data:", "javascript:", "mailto:", "tel:", "#", "about:")):
            return None

        absolute = normalize_url(raw, base_url)
        if not is_in_scope(absolute, self.state.allowed_hosts):
            # Out of scope: leave as absolute so it still works online.
            return absolute

        # Decide what kind of resource this is.
        if tag_name == "a" or (tag_name == "iframe" and attr == "src"):
            kind = "page"
        elif tag_name == "link":
            ext = os.path.splitext(urlparse(absolute).path)[1].lower()
            if ext == ".css":
                kind = "css"
            elif ext in IMAGE_EXTS or ext in SKIP_COMPRESS_EXTS:
                kind = "image"
            else:
                kind = "other"
        elif tag_name in {"img", "source", "video", "audio", "track", "embed", "object"}:
            kind = "image" if tag_name in {"img", "source"} else classify_asset(absolute)
        elif tag_name == "script":
            kind = "js"
        elif tag_name == "form":
            # Forms don't really work offline; leave as absolute.
            return absolute
        else:
            kind = classify_asset(absolute)

        # Enqueue if we haven't seen it.
        if absolute not in self.state.visited:
            if kind == "page":
                if self.args.max_depth < 0 or depth + 1 <= self.args.max_depth:
                    self._enqueue(absolute, depth + 1, "page")
                else:
                    # Over depth: don't fetch, but don't break the link either.
                    return absolute
            else:
                self._enqueue(absolute, depth, kind)

        # Compute the local path we WILL use for this URL.
        is_start = absolute == self.state.start_url
        local = self.state.visited.get(absolute) or url_to_local_path(
            absolute, kind, is_start=is_start
        )
        return rel_link(page_local, local)

    def _rewrite_srcset(self, value: str, base_url: str, page_local: str, depth: int) -> str:
        out_parts = []
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split(None, 1)
            url_part = bits[0]
            descriptor = f" {bits[1]}" if len(bits) > 1 else ""
            new = self._rewrite_single_url(
                url_part, base_url, page_local, tag_name="img", attr="src", depth=depth
            )
            out_parts.append(f"{new or url_part}{descriptor}")
        return ", ".join(out_parts)

    def _rewrite_css_text(self, text: str, base_url: str, local_path: str) -> str:
        """Rewrite url(...) and @import in a CSS string."""
        def url_sub(match: re.Match) -> str:
            quote = match.group(1) or ""
            raw = match.group(2).strip()
            if not raw or raw.startswith(("data:", "#")):
                return match.group(0)
            absolute = normalize_url(raw, base_url)
            if not is_in_scope(absolute, self.state.allowed_hosts):
                return match.group(0)
            kind = classify_asset(absolute)
            if absolute not in self.state.visited:
                self._enqueue(absolute, 0, kind)
            target = self.state.visited.get(absolute) or url_to_local_path(
                absolute, kind
            )
            rel = rel_link(local_path, target)
            return f"url({quote}{rel}{quote})"

        text = re.sub(
            r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""",
            url_sub,
            text,
        )

        def import_sub(match: re.Match) -> str:
            quote = match.group(1)
            raw = match.group(2).strip()
            absolute = normalize_url(raw, base_url)
            if not is_in_scope(absolute, self.state.allowed_hosts):
                return match.group(0)
            if absolute not in self.state.visited:
                self._enqueue(absolute, 0, "css")
            target = self.state.visited.get(absolute) or url_to_local_path(
                absolute, "css"
            )
            rel = rel_link(local_path, target)
            return f"@import {quote}{rel}{quote}"

        text = re.sub(
            r"""@import\s+(['"])([^'"]+)\1""",
            import_sub,
            text,
        )
        return text

    # ----------------------------- assets -------------------------------- #

    def _process_asset(self, url: str, kind: str) -> None:
        resp = self._fetch(url)
        if resp is None:
            return
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        # Refine classification using the server's Content-Type.
        refined = classify_asset(url, ctype)
        if kind == "other" and refined != "other":
            kind = refined
        self._save_asset_bytes(url, resp.content, kind, ctype)

    def _save_asset_bytes(
        self, url: str, data: bytes, kind: str, ctype: str
    ) -> None:
        local = url_to_local_path(url, kind)

        if kind == "image" and not self.args.no_compress:
            new_data, new_ext = compress_image(
                data, url, self.args.image_max_dim, self.args.image_quality
            )
            if new_data is not data and new_data != data:
                self.state.stats.images_compressed += 1
                # Update extension in the local path if format changed.
                root, old_ext = os.path.splitext(local)
                if new_ext and new_ext.lower() != old_ext.lower():
                    local = root + new_ext
                data = new_data
        elif kind == "css":
            try:
                text = data.decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                text = data.decode("latin-1", errors="replace")
            text = self._rewrite_css_text(text, url, local)
            data = text.encode("utf-8")

        out_path = self.output / local
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)

        self.state.visited[url] = local
        self.state.stats.assets_saved += 1
        self.state.stats.bytes_written += len(data)
        self.log.debug("asset %s (%s) %s -> %s", kind, ctype, url, local)

    # ----------------------------- queue --------------------------------- #

    def _enqueue(self, url: str, depth: int, kind: str) -> None:
        if url in self.state.visited:
            return
        if not is_in_scope(url, self.state.allowed_hosts):
            return
        # Dedupe against pending queue entries.
        # (linear but queues are bounded by configured limits)
        for existing in self.queue:
            if existing[0] == url:
                return
        self.queue.append((url, depth, kind))

    # ----------------------------- summary ------------------------------- #

    def _log_summary(self) -> None:
        s = self.state.stats
        self.log.info("--- Backup summary ---")
        self.log.info("Pages saved:       %d", s.pages_saved)
        self.log.info("Assets saved:      %d", s.assets_saved)
        self.log.info("Images compressed: %d", s.images_compressed)
        self.log.info("Bytes downloaded:  %d", s.bytes_downloaded)
        self.log.info("Bytes written:     %d", s.bytes_written)
        self.log.info("Failed URLs:       %d", s.failed)
        self.log.info("Queue remaining:   %d", len(self.queue))
        self.log.info("Output: %s", self.output)
        self.log.info("Open %s/index.html to browse offline.", self.output)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kiwieater",
        description="Archive a website (forum-friendly) into a self-contained "
                    "offline-browsable folder. Supports resume.",
    )
    p.add_argument("url", nargs="?", help="Seed URL. Omit when resuming.")
    p.add_argument(
        "-o", "--output", default="backup_output",
        help="Output directory (default: backup_output).",
    )
    p.add_argument(
        "--allowed-hosts", nargs="*", default=None,
        help="Additional hostnames to treat as in-scope. Subdomains of the "
             "seed host are always in scope.",
    )
    p.add_argument(
        "--max-depth", type=int, default=5,
        help="Max link-depth from the seed page (-1 = unlimited). Default 5.",
    )
    p.add_argument(
        "--max-pages", type=int, default=0,
        help="Stop after this many pages (0 = unlimited).",
    )
    p.add_argument(
        "--delay", type=float, default=0.5,
        help="Seconds to sleep between requests (politeness).",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0,
        help="Per-request timeout in seconds.",
    )
    p.add_argument(
        "--user-agent", default="KiwiEater/1.0 (+offline-archive)",
        help="User-Agent header.",
    )
    p.add_argument(
        "--cookies", default=None,
        help="Path to a Netscape-format cookies.txt (for authenticated forums).",
    )
    p.add_argument(
        "--image-max-dim", type=int, default=1280,
        help="Resize images so their longest side is <= this (pixels).",
    )
    p.add_argument(
        "--image-quality", type=int, default=72,
        help="JPEG quality for recompressed images (1-95).",
    )
    p.add_argument(
        "--no-compress", action="store_true",
        help="Disable image recompression / resizing.",
    )
    p.add_argument(
        "--save-every", type=int, default=10,
        help="Flush state to disk every N items (default 10).",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging.",
    )
    return p


def configure_logging(output_dir: Path, verbose: bool) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("kiwieater")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(output_dir / LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO if not verbose else logging.DEBUG)
    logger.addHandler(sh)

    return logger


def main(argv: Optional[list[str]] = None) -> int:
    if sys.platform != "linux":
        print("kiwieater: this tool is Linux-only.", file=sys.stderr)
        return 2

    args = build_parser().parse_args(argv)
    output = Path(args.output).resolve()
    log = configure_logging(output, args.verbose)

    state_path = output / STATE_FILE
    if not args.url and not state_path.exists():
        print("kiwieater: URL is required for a new backup "
              "(no state file found in output directory).", file=sys.stderr)
        return 2

    # Allow resume without re-passing the URL.
    if not args.url:
        with state_path.open("r", encoding="utf-8") as f:
            args.url = json.load(f)["start_url"]
        log.info("Resuming existing backup for %s", args.url)

    try:
        Backupper(args, log).run()
    except KeyboardInterrupt:
        log.warning("Interrupted. Re-run the same command to resume.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
