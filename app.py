#!/usr/bin/env python3
"""
Musick — a self-contained YouTube & Tubidy downloader with the Musick AI assistant.

Upgrades & Fixes (v2):
1. Persistent Chat State & Inline Results:
   - Exiting/closing the chat panel preserves all messages, inline search results, and active download cards.
   - `inlineResults` are serialized to `localStorage` so they survive chat closes and page reloads.
2. Faster Downloads & Direct Device Delivery:
   - Skips slow Tubidy scraping by default for YouTube/general queries and goes straight to fast `yt-dlp` with `concurrent_fragment_downloads=4`, `socket_timeout=8`, and `extractor_args` for instant stream resolution.
   - Exports completed files into the configured `outdir` (e.g., `/sdcard/Download/MP3Hub` or `~/Downloads/MP3Hub`), triggers browser file download automatically (`auto_save`), and indexes all downloaded files in `/api/library` so they are always visible and playable in-site.
3. Dedicated Downloads & Library Tab + Full In-Site Player:
   - Top navigation tabs: **Discover** (Search + Queue) and **Library & Downloads** (Saved Files + Queue + In-Site Audio/Video Player with Playlist Queue, Scrubbing, Speed, Loop, Shuffle, Sleep Timer, and Keyboard Controls).
4. True YouTube-Ranked Search Results:
   - Preserves YouTube's native relevance & popularity order (exact song matches, remixes, covers, slowed/reverb, and popular uploads) instead of filtering or skewing toward generic artist channels.
5. Smarter Musick AI (Clarifying Questions + Direct `yt-dlp` Downloads):
   - System prompt instructs Musick AI to ask helpful clarifying questions when a song query is ambiguous (e.g., original vs. remix/slowed/live, or MP3 vs. MP4), search YouTube accurately, and execute `{"action": "download_query", "query": "...", "mode": "mp3"}` or `{"action": "download", "url": "...", "mode": "mp3"}` immediately when told "download it for me".
6. 20 New Features Included:
   1. Dedicated Discover / Library & Downloads navigation tabs
   2. Persistent in-site Audio & Video Media Player bar + Queue drawer
   3. Playback speed control (0.75x, 1x, 1.25x, 1.5x, 2x)
   4. Loop modes (Off / Repeat All / Repeat One) & Shuffle play
   5. Sleep timer (15m / 30m / 60m / End of track)
   6. Live Web Audio API frequency visualizer bars in the player
   7. Direct AI `download_query` action (resolves top YouTube match via yt-dlp and downloads immediately)
   8. AI clarifying follow-up suggestions & song disambiguation
   9. Persistent AI chat inline cards across chat close/open & reloads
   10. Library search & sort (Newest, Name, Size, Format)
   11. "Play All" & "Shuffle All" from downloaded library
   12. Quick-select Trending / Genre discovery pills
   13. One-click "Download Top Match" button on search bar
   14. Favorites Manager drawer/filter
   15. Concurrent fragment downloading (`concurrent_fragment_downloads=4`) for faster yt-dlp speeds
   16. Custom filename template setting (`%(title)s`, `%(artist)s - %(title)s`)
   17. Real-time library file scanner (`/api/library`) showing exact saved file path & size
   18. In-site Video Modal / Theater Player for downloaded MP4s
   19. Keyboard media controls (`Space` play/pause, `ArrowLeft`/`ArrowRight` seek ±5s)
   20. Batch-download all search results or selected favorites in one click
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
import html as _html
import shutil
import socket
import queue
import threading
from pathlib import Path
from urllib.parse import quote_plus

from flask import Flask, request, jsonify, Response, send_file, stream_with_context, abort

try:
    import requests
except ImportError:
    print("Missing dependency: requests. Install with: pip install requests")
    sys.exit(1)

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    yt_dlp = None
    HAS_YTDLP = False

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


# ---------------------------------------------------------------------------
# Paths & settings
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

TUBIDY_BASE = "https://tubidy.com"
TUBIDY_SEARCH_URL = TUBIDY_BASE + "/search?q={q}"
TUBIDY_UA = (
    "Mozilla/5.0 (Linux; Android 10; SM-G975F) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

DATA_DIR = BASE_DIR / "mp3hub_data"
JOBS_DIR = DATA_DIR / "jobs"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_GEMINI_KEY = "AQ.Ab8RN6KrTaZ3e5QZoOd5yBmxBPwfwWvWrV405HYkLlz5CXsz5w"


def _default_outdir() -> str:
    sd = Path("/sdcard/Download")
    if sd.exists():
        return str(sd / "MP3Hub")
    return str(Path.home() / "Downloads" / "MP3Hub")


DEFAULT_SETTINGS = {
    "theme": "dark",
    "accent": "#4d6bfe",
    "mode": "mp3",
    "height": "720",
    "abitrate": "192",
    "cookies": "",
    "proxy": "",
    "retries": 2,
    "concurrency": 4,
    "embed_thumb": True,
    "embed_meta": True,
    "subs": False,
    "outdir": _default_outdir(),
    "autoclean_days": 7,
    "rate_limit": "",
    "auto_save": True,
    "notify_done": True,
    "auto_retry": True,
    "theme_auto": False,
    "density": "comfortable",
    "playlist_limit": 20,
    "sponsorblock": False,
    "normalize": False,
    "video_codec": "any",
    "filename_template": "%(title).180B [%(id)s].%(ext)s",
    "gemini_key": "",
    "chat_mode": "fast",
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    s[k] = v
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    clean = {k: s.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    try:
        SETTINGS_FILE.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    except Exception as e:
        print("! could not save settings:", e)


def load_history() -> dict:
    h = {"searches": [], "downloads": []}
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("searches"), list):
                h["searches"] = data["searches"]
            if isinstance(data.get("downloads"), list):
                h["downloads"] = data["downloads"]
    except Exception:
        pass
    return h


def save_history(h: dict) -> None:
    try:
        HISTORY_FILE.write_text(json.dumps(h, indent=2), encoding="utf-8")
    except Exception:
        pass


def add_search_history(q: str) -> None:
    h = load_history()
    h["searches"] = [q] + [s for s in h["searches"] if s != q]
    h["searches"] = h["searches"][:50]
    save_history(h)


def add_download_history(entry: dict) -> None:
    h = load_history()
    h["downloads"] = [entry] + h["downloads"][:199]
    save_history(h)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def fmt_duration(sec) -> str:
    if not sec:
        return "?"
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_bytes(n) -> str:
    if not n:
        return "0B"
    try:
        n = float(n)
    except Exception:
        return "0B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_speed(n) -> str:
    if not n:
        return ""
    return fmt_bytes(n) + "/s"


def fmt_eta(sec) -> str:
    if sec is None:
        return ""
    try:
        sec = int(sec)
    except Exception:
        return ""
    if sec < 60:
        return f"0:{sec:02d}"
    m, s = sec // 60, sec % 60
    if m < 60:
        return f"{m}:{s:02d}"
    h, m = m // 60, m % 60
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Fast Job Engine
# ---------------------------------------------------------------------------
JOBS: dict = {}
JOBS_LOCK = threading.Lock()


class JobLimiter:
    def __init__(self, n: int):
        self.n = max(1, int(n))
        self.active = 0
        self.cv = threading.Condition()

    def set(self, n: int) -> None:
        with self.cv:
            self.n = max(1, int(n))
            self.cv.notify_all()

    def acquire(self, timeout=None) -> bool:
        with self.cv:
            if timeout is None:
                while self.active >= self.n:
                    self.cv.wait()
                self.active += 1
                return True
            end = time.time() + timeout
            while self.active >= self.n:
                remaining = end - time.time()
                if remaining <= 0:
                    return False
                self.cv.wait(min(remaining, 1.0))
            self.active += 1
            return True

    def release(self) -> None:
        with self.cv:
            self.active = max(0, self.active - 1)
            self.cv.notify_all()


LIMITER = JobLimiter(load_settings().get("concurrency", 4))


def _job_watchdog():
    while True:
        time.sleep(25)
        try:
            now = time.time()
            with JOBS_LOCK:
                for j in JOBS.values():
                    if j.get("status") in ("starting", "downloading", "processing"):
                        lu = j.get("last_update") or j.get("created") or now
                        if now - lu > 240:
                            j["status"] = "error"
                            j["error"] = "Stalled — no progress for 4 minutes"
        except Exception:
            continue


threading.Thread(target=_job_watchdog, daemon=True).start()


def public_job(j: dict) -> dict:
    return {
        "id": j["id"],
        "url": j.get("url"),
        "title": j.get("title") or j.get("url") or "",
        "thumb": j.get("thumb"),
        "channel": j.get("channel"),
        "mode": j.get("mode"),
        "height": j.get("height"),
        "abitrate": j.get("abitrate"),
        "status": j.get("status"),
        "stage": j.get("stage", ""),
        "progress": j.get("progress", 0),
        "downloaded": j.get("downloaded", 0),
        "total": j.get("total", 0),
        "speed": j.get("speed", ""),
        "eta": j.get("eta", ""),
        "error": j.get("error"),
        "file": j.get("file"),
        "exported_file": j.get("exported_file"),
        "created": j.get("created"),
        "retried": j.get("retry_count", 0),
    }


def make_progress_hook(job: dict):
    def hook(d):
        if job.get("cancelled"):
            raise RuntimeError("cancelled")
        job["last_update"] = time.time()
        st = d.get("status")
        if st == "downloading":
            if job.get("status") in ("error", "done", "cancelled"):
                raise RuntimeError("cancelled")
            job["status"] = "downloading"
            job["stage"] = "Downloading stream"
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            dl = d.get("downloaded_bytes") or 0
            try:
                job["downloaded"] = int(dl)
                job["total"] = int(total)
                job["progress"] = round((dl / total) * 100, 1) if total else 0
                job["speed"] = fmt_speed(d.get("speed"))
                job["eta"] = fmt_eta(d.get("eta"))
            except Exception:
                pass
        elif st == "finished":
            job["status"] = "processing"
            job["stage"] = "Converting & tagging"
            job["progress"] = 99.0
    return hook


def make_postprocessor_hook(job: dict):
    def pphook(d):
        if job.get("cancelled"):
            raise RuntimeError("cancelled")
        job["last_update"] = time.time()
        st = d.get("status")
        pp = d.get("postprocessor") or ""
        if st in ("started", "processing"):
            job["status"] = "processing"
            if "ExtractAudio" in pp:
                job["stage"] = "Extracting MP3 audio"
            elif "EmbedThumbnail" in pp:
                job["stage"] = "Embedding cover art"
            elif "Metadata" in pp:
                job["stage"] = "Writing metadata"
            else:
                job["stage"] = "Processing media"
    return pphook


def default_video_string(height, codec="any") -> str:
    filt = ""
    if codec == "h264":
        filt = "[vcodec^=avc]"
    elif codec == "h265":
        filt = "[vcodec^=hev]"
    elif codec == "vp9":
        filt = "[vcodec^=vp9]"
    elif codec == "av1":
        filt = "[vcodec^=av01]"
    if height and str(height) != "best":
        try:
            h = int(height)
            return (
                f"bv*{filt}[height<={h}][ext=mp4]+ba[ext=m4a]/"
                f"b[height<={h}][ext=mp4]/"
                f"bv*{filt}[height<={h}]+ba/"
                f"b[height<={h}]/b"
            )
        except Exception:
            pass
    return f"bv*{filt}+ba/b"


def build_format_string(mode, height, format_id=None, format_acodec=None, codec="any") -> str:
    if mode == "mp3":
        return "bestaudio[ext=m4a]/bestaudio/best"
    base = default_video_string(height, codec)
    if format_id:
        if format_acodec == "none":
            return f"{format_id}+ba/{base}"
        return f"{format_id}/{base}"
    return base


def build_ydl_opts(job: dict) -> dict:
    settings = load_settings()
    user_retries = max(1, int(settings.get("retries", 2)))
    tmpl = settings.get("filename_template") or "%(title).180B [%(id)s].%(ext)s"
    if "%(ext)s" not in tmpl:
        tmpl = tmpl + ".%(ext)s"

    opts: dict = {
        "outtmpl": os.path.join(job["dir"], tmpl),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [make_progress_hook(job)],
        "postprocessor_hooks": [make_postprocessor_hook(job)],
        "noplaylist": not bool(job.get("playlist")),
        "retries": user_retries,
        "fragment_retries": user_retries,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size": 1048576,
        "ignoreerrors": False,
        "writethumbnail": bool(settings.get("embed_thumb", True)),
        "socket_timeout": 8,
        "extractor_retries": 1,
        "source_address": "0.0.0.0",
        "geo_bypass": True,
    }
    if settings.get("cookies"):
        opts["cookiefile"] = settings["cookies"]
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    if settings.get("rate_limit"):
        opts["ratelimit"] = settings["rate_limit"]
    if job.get("playlist") and job.get("playlist_limit"):
        try:
            opts["playlistend"] = int(job["playlist_limit"])
        except Exception:
            pass

    pps = []

    if job["mode"] == "mp3":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        pps.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(job.get("abitrate") or "192"),
        })
        if settings.get("normalize"):
            opts["postprocessor_args"] = {"extractaudio": ["-af", "dynaudnorm=p=0.9:m=10"]}
    else:
        opts["format"] = build_format_string(
            "mp4", job.get("height"), job.get("format_id"),
            job.get("format_acodec"), settings.get("video_codec", "any")
        )
        opts["merge_output_format"] = "mp4"

    if settings.get("sponsorblock"):
        cats = ["sponsor", "selfpromo", "interaction", "intro", "outro", "music_offtopic"]
        pps.append({"key": "SponsorBlock", "categories": cats})
        pps.append({"key": "ModifyChapters", "remove_sponsor_segments": cats})

    if settings.get("embed_thumb"):
        pps.append({"key": "EmbedThumbnail"})
    if settings.get("embed_meta"):
        pps.append({"key": "FFmpegMetadata"})
    if settings.get("subs"):
        pps.append({"key": "FFmpegEmbedSubtitle"})
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = ["en"]

    if pps:
        opts["postprocessors"] = pps
    return opts


def pick_output(job_dir: str, mode: str):
    want = ".mp3" if mode == "mp3" else ".mp4"
    ignore_ext = (".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp",
                  ".info.json", ".vtt", ".srt", ".tmp")
    best = None
    best_mtime = 0.0
    for root, _dirs, files in os.walk(job_dir):
        for f in files:
            if not f.lower().endswith(want):
                continue
            if any(f.lower().endswith(x) for x in ignore_ext):
                continue
            p = os.path.join(root, f)
            try:
                mt = os.path.getmtime(p)
            except Exception:
                continue
            if mt > best_mtime:
                best_mtime = mt
                best = p
    return best


def _copy_to_outdir(src_path: str) -> str | None:
    """Copy completed file to configured output directory."""
    src = Path(src_path)
    if not src.exists():
        return None
    exported = None
    try:
        settings = load_settings()
        outdir = settings.get("outdir")
        if outdir:
            dest_dir = Path(outdir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if dest.resolve() != src.resolve():
                shutil.copy2(str(src), str(dest))
            exported = str(dest)
    except Exception as e:
        print("[outdir] copy skipped:", e)
    return exported


def _resolve_top_youtube_match(query: str) -> dict | None:
    """Fast flat lookup of the top YouTube result for a query."""
    if not HAS_YTDLP:
        return None
    search_q = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": 5,
        "ignoreerrors": True,
        "noplaylist": True,
        "socket_timeout": 8,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_q, download=False)
        entries = info.get("entries") if info and info.get("_type") == "playlist" else ([info] if info else [])
        for e in (entries or []):
            if not e:
                continue
            ie_key = (e.get("ie_key") or "").lower()
            if "channel" in ie_key or "playlist" in ie_key or "tab" in ie_key:
                continue
            vid = e.get("id") or ""
            title = (e.get("title") or "").strip()
            if not vid or not title:
                continue
            url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}"
            if not ("youtube.com" in url or "youtu.be" in url):
                url = f"https://www.youtube.com/watch?v={vid}"
            thumb = e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            return {
                "id": vid,
                "title": title,
                "url": url,
                "channel": e.get("channel") or e.get("uploader") or "",
                "duration": e.get("duration"),
                "thumbnail": thumb,
            }
    except Exception as e:
        print("[resolve_top] error:", e)
    return None


def run_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    if job.get("cancelled"):
        job["status"] = "cancelled"
        return
    LIMITER.acquire()
    try:
        if job.get("cancelled"):
            job["status"] = "cancelled"
            return
        job["status"] = "starting"
        job["stage"] = "Connecting to stream"
        job["last_update"] = time.time()

        # If a query was passed without a URL, resolve top YouTube match via yt-dlp immediately
        if not job.get("url") and job.get("query"):
            job["stage"] = f"Finding '{job['query']}'"
            match = _resolve_top_youtube_match(job["query"])
            if not match:
                raise RuntimeError(f"Could not find YouTube match for: {job['query']}")
            job["url"] = match["url"]
            job["title"] = job.get("title") or match["title"]
            job["thumb"] = job.get("thumb") or match["thumbnail"]
            job["channel"] = job.get("channel") or match["channel"]
            job["duration"] = job.get("duration") or match["duration"]

        # Only use Tubidy if URL explicitly points to tubidy.com
        vid = job.get("video_id")
        is_tubidy = "tubidy.com" in (job.get("url") or "")
        if is_tubidy and not vid:
            m = re.search(r"/watch/([A-Za-z0-9_=-]+)", job["url"])
            if m:
                vid = m.group(1)

        ok = False
        if is_tubidy and vid and job.get("mode") == "mp3":
            job["status"] = "downloading"
            job["stage"] = "Downloading from Tubidy"
            path = tubidy_download(vid, job["dir"], job.get("title"), job=job)
            if path:
                job["file"] = path
                job["progress"] = 100
                job["stage"] = "Completed"
                job["status"] = "done"
                ok = True

        # Fast yt-dlp primary engine
        if not ok:
            if not HAS_YTDLP:
                raise RuntimeError("yt-dlp is not installed on the server")
            job["status"] = "starting"
            job["stage"] = "Fetching stream"
            opts = build_ydl_opts(job)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job["url"], download=True)
                if info and not job.get("title"):
                    job["title"] = info.get("title") or job["url"]
                if info and not job.get("thumb"):
                    job["thumb"] = info.get("thumbnail")
                if info and not job.get("channel"):
                    job["channel"] = info.get("channel") or info.get("uploader")
            job["file"] = pick_output(job["dir"], job["mode"])
            if not job["file"]:
                raise RuntimeError("Output file was not produced after download")
            job["progress"] = 100
            job["stage"] = "Completed"
            job["status"] = "done"

        if job.get("cancelled"):
            job["status"] = "cancelled"
        else:
            job["status"] = "done"
            if job.get("file"):
                exported = _copy_to_outdir(job["file"])
                if exported:
                    job["exported_file"] = exported
                add_download_history({
                    "id": job["id"],
                    "title": job.get("title") or job.get("url"),
                    "url": job.get("url"),
                    "channel": job.get("channel"),
                    "thumb": job.get("thumb"),
                    "mode": job.get("mode"),
                    "file": job.get("exported_file") or job["file"],
                    "time": time.time(),
                })
    except Exception as e:
        if job.get("cancelled"):
            job["status"] = "cancelled"
        else:
            settings = load_settings()
            if settings.get("auto_retry") and job.get("retry_count", 0) < 1:
                job["retry_count"] = job.get("retry_count", 0) + 1
                job["status"] = "starting"
                job["stage"] = "Auto-retrying..."
                job["error"] = None
                LIMITER.release()
                time.sleep(1.0)
                run_job(job_id)
                return
            job["status"] = "error"
            job["stage"] = "Failed"
            job["error"] = str(e)[:500]
    finally:
        try:
            LIMITER.release()
        except Exception:
            pass


def create_job(params: dict) -> dict:
    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "url": params.get("url"),
        "query": params.get("query"),
        "video_id": params.get("video_id"),
        "mode": params.get("mode", "mp3"),
        "height": params.get("height"),
        "abitrate": params.get("abitrate"),
        "format_id": params.get("format_id"),
        "format_acodec": params.get("format_acodec"),
        "playlist": bool(params.get("playlist")),
        "playlist_limit": params.get("playlist_limit") or load_settings().get("playlist_limit", 20),
        "title": params.get("title") or params.get("query"),
        "thumb": params.get("thumb"),
        "channel": params.get("channel"),
        "duration": params.get("duration"),
        "status": "starting",
        "stage": "Starting immediately",
        "progress": 0,
        "downloaded": 0,
        "total": 0,
        "speed": "",
        "eta": "",
        "error": None,
        "file": None,
        "exported_file": None,
        "dir": str(job_dir),
        "created": time.time(),
        "cancelled": False,
        "retry_count": 0,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return job


# ---------------------------------------------------------------------------
# Tubidy scraper (fallback / explicit tubidy URLs)
# ---------------------------------------------------------------------------
TUBIDY_SESSION = requests.Session()
TUBIDY_SESSION.headers.update({
    "User-Agent": TUBIDY_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

RE_RESULT = re.compile(
    r'<a[^>]+href="(/watch/[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>',
    re.S | re.I,
)
RE_MP3_LINK = re.compile(
    r'<a[^>]+href="([^"]*(?:\.mp3|download[^"]*))"[^>]*>',
    re.I,
)
RE_VIDEO_ID = re.compile(r'/watch/([A-Za-z0-9_=-]+)')


def tubidy_search(query: str, limit: int = 12):
    url = TUBIDY_SEARCH_URL.format(q=requests.utils.quote(query))
    try:
        r = TUBIDY_SESSION.get(url, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("[tubidy] search error:", e)
        return []

    out = []
    seen = set()
    for href, title in RE_RESULT.findall(r.text):
        vid = RE_VIDEO_ID.search(href)
        if not vid:
            continue
        vid_str = vid.group(1)
        if vid_str in seen:
            continue
        seen.add(vid_str)
        clean_title = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if not clean_title:
            continue
        out.append({
            "id": vid_str,
            "title": clean_title,
            "url": TUBIDY_BASE + href,
            "duration": None,
            "thumbnail": None,
            "channel": "Tubidy",
        })
        if len(out) >= limit:
            break
    return out


def tubidy_resolve(video_id: str) -> dict | None:
    page_url = f"{TUBIDY_BASE}/watch/{video_id}"
    try:
        r = TUBIDY_SESSION.get(page_url, timeout=12)
        r.raise_for_status()
    except Exception:
        return None

    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", r.text, re.S | re.I)
    title = _html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else "Untitled"

    mp3 = None
    mp4 = None
    for href in RE_MP3_LINK.findall(r.text):
        full = href if href.startswith("http") else TUBIDY_BASE + href
        if ".mp3" in href.lower():
            mp3 = full
            break
        if not mp4 and ("mp4" in href.lower() or "video" in href.lower()):
            mp4 = full

    if not mp3:
        return None

    return {"title": title, "mp3_url": mp3, "mp4_url": mp4, "page_url": page_url}


def tubidy_download(video_id: str, out_dir: str, filename_hint: str | None = None, job: dict | None = None) -> str | None:
    info = tubidy_resolve(video_id)
    if not info:
        return None
    if job and not job.get("title"):
        job["title"] = info["title"]
    safe = re.sub(r'[\\/:*?"<>|]', "_", filename_hint or info["title"])[:120]
    dest = os.path.join(out_dir, safe + ".mp3")
    try:
        with TUBIDY_SESSION.get(info["mp3_url"], stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            t0 = time.time()
            if job:
                job["total"] = total
                job["last_update"] = t0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    if job and job.get("cancelled"):
                        raise RuntimeError("cancelled")
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if job:
                            now = time.time()
                            elapsed = max(0.05, now - t0)
                            spd = done / elapsed
                            job["downloaded"] = done
                            job["total"] = total
                            job["last_update"] = now
                            job["speed"] = fmt_speed(spd)
                            if total > 0:
                                job["progress"] = round((done / total) * 100, 1)
                                rem = max(0, total - done)
                                job["eta"] = fmt_eta(rem / spd) if spd > 0 else ""
    except Exception:
        return None
    return dest


# ---------------------------------------------------------------------------
# Flask app & Upgraded Musick AI Assistant
# ---------------------------------------------------------------------------
app = Flask(__name__)

MUSICK_SYSTEM_PROMPT = (
    "You are Musick AI, an intelligent, music-savvy assistant built inside a fast YouTube & Tubidy media app powered by yt-dlp.\n"
    "\n"
    "YOUR ROLE & HOW THE APP WORKS:\n"
    "- You have REAL search and download powers via JSON action blocks at the end of your reply. The app executes them immediately using yt-dlp.\n"
    "- NEVER say 'I don't know', 'I cannot download', or 'not found'. You CAN search YouTube and you CAN trigger yt-dlp downloads directly!\n"
    "\n"
    "OUTPUT FORMAT (strictly follow):\n"
    "1. FIRST reason inside a single <thinking>...</thinking> block (2-4 short sentences).\n"
    "   - Understand what song/artist/version the user wants.\n"
    "   - Decide if the user wants to SEARCH (browse results/remixes/versions) OR DOWNLOAD directly ('download it for me', 'grab that', 'save it').\n"
    "   - Also plan 1 helpful, natural follow-up question about the song (e.g., asking if they want the original, a remix, slowed/reverb, lyrics video, or MP4 video instead of MP3).\n"
    "2. AFTER </thinking>, write a warm, concise reply (2-4 short sentences). ALWAYS include a helpful question about the song (e.g., asking which version/remix or format they prefer, or offering related tracks).\n"
    "3. APPEND one or more JSON action blocks at the very end when appropriate:\n"
    "\n"
    "To search YouTube (shows real ranked YouTube cards including originals, remixes, and covers):\n"
    "```json\n"
    '{"action": "search", "query": "Pisces Central Cee"}\n'
    "```\n"
    "\n"
    "To immediately download a song via yt-dlp when the user says 'download it for me' or asks to download a specific song:\n"
    "```json\n"
    '{"action": "download_query", "query": "Pisces Central Cee", "mode": "mp3"}\n'
    "```\n"
    "\n"
    "To download a specific URL already shown in context:\n"
    "```json\n"
    '{"action": "download", "url": "https://www.youtube.com/watch?v=...", "mode": "mp3"}\n'
    "```\n"
    "\n"
    "SEARCH & CONVERSATION RULES:\n"
    "- Keep search queries natural like a human types into YouTube (e.g. `Pisces Central Cee`, NOT overly restrictive filters) so YouTube returns the original track, remixes by other artists, and popular versions.\n"
    "- When the user says 'download it for me' or 'download that song', check [App Context] for the last searched/discussed song or URL and emit `download` (if URL known) or `download_query` (with the song title) so yt-dlp starts downloading immediately!\n"
    "- Always ask a brief, engaging question about the song in your response (e.g. 'Would you like me to download the official audio as MP3, or are you looking for a specific remix or slowed version?').\n"
)


def _build_dynamic_system_prompt() -> str:
    settings = load_settings()
    hist = load_history()
    recent_searches = ", ".join((hist.get("searches") or [])[:5]) or "none"
    with JOBS_LOCK:
        active_jobs = [
            f"{j.get('title') or j.get('url')} ({j.get('status')} {j.get('progress', 0)}%)"
            for j in list(JOBS.values())[-5:]
        ]
    jobs_summary = "; ".join(active_jobs) or "none"
    ctx = (
        f"\n[App Context]\n"
        f"- User default mode: {settings.get('mode', 'mp3').upper()} "
        f"({settings.get('abitrate', '192')}kbps / {settings.get('height', '720')}p)\n"
        f"- Recent searches: {recent_searches}\n"
        f"- Recent/active downloads: {jobs_summary}\n"
    )
    return MUSICK_SYSTEM_PROMPT + ctx


def _resolve_llm(key_override=None):
    settings = load_settings()
    gemini_key = (key_override or os.environ.get("GEMINI_API_KEY")
                  or settings.get("gemini_key") or DEFAULT_GEMINI_KEY)
    endpoints = []
    if gemini_key:
        endpoints.append({
            "name": "primary",
            "endpoint": os.environ.get("MUSICK_ENDPOINT")
                        or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "model": os.environ.get("MUSICK_MODEL") or "gemini-2.5-flash",
            "key": gemini_key,
        })
    endpoints.append({
        "name": "fallback",
        "endpoint": "https://text.pollinations.ai/openai",
        "model": "openai",
        "key": None,
    })
    return endpoints


_LLM_CACHE = {"winner": None, "ts": 0.0}
_LLM_LOCK = threading.Lock()


def _stream_from_provider(ep, full_messages):
    headers = {"Content-Type": "application/json"}
    if ep["key"]:
        headers["Authorization"] = f"Bearer {ep['key']}"
    payload = {
        "model": ep["model"],
        "messages": full_messages,
        "stream": True,
        "temperature": 0.65,
        "max_tokens": 1000,
    }
    r = requests.post(ep["endpoint"], json=payload, headers=headers,
                      stream=True, timeout=(6, 25))
    if r.status_code >= 400:
        try:
            body = r.text[:120]
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {r.status_code} {body}")
    got = False
    for raw in r.iter_lines(decode_unicode=True):
        if not raw:
            continue
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", "replace")
            except Exception:
                continue
        if not raw.startswith("data: "):
            continue
        data = raw[6:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        try:
            c = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if c:
                got = True
                yield c
        except Exception:
            continue
    if not got:
        raise RuntimeError("empty stream")


def _race_stream(endpoints, full_messages, deadline=25):
    q = queue.Queue()
    winner = {"name": None}
    errors = {}

    def worker(ep):
        try:
            for tok in _stream_from_provider(ep, full_messages):
                q.put(("tok", ep["name"], tok))
            q.put(("done", ep["name"], None))
        except Exception as e:
            q.put(("err", ep["name"], str(e)[:120]))

    for ep in endpoints:
        threading.Thread(target=worker, args=(ep,), daemon=True).start()

    deadline_ts = time.time() + deadline
    while time.time() < deadline_ts:
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            continue
        typ, name, payload = item
        if typ == "tok":
            if winner["name"] is None:
                winner["name"] = name
                with _LLM_LOCK:
                    _LLM_CACHE["winner"] = name
                    _LLM_CACHE["ts"] = time.time()
            if name == winner["name"]:
                yield payload
        elif typ == "done":
            if name == winner["name"]:
                return
        elif typ == "err":
            errors[name] = payload
            if winner["name"] is None and len(errors) >= len(endpoints):
                raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    if winner["name"] is None:
        raise RuntimeError("LLM race timed out (25s)")


def _llm_iter(messages):
    sys_prompt = _build_dynamic_system_prompt()
    full_messages = [{"role": "system", "content": sys_prompt}] + list(messages)
    endpoints = _resolve_llm()

    with _LLM_LOCK:
        cache_winner = _LLM_CACHE.get("winner")
        cache_ts = _LLM_CACHE.get("ts", 0)
    if cache_winner and (time.time() - cache_ts) < 300:
        preferred = next((ep for ep in endpoints if ep["name"] == cache_winner), None)
        if preferred:
            try:
                for tok in _stream_from_provider(preferred, full_messages):
                    yield tok
                return
            except Exception:
                with _LLM_LOCK:
                    _LLM_CACHE["winner"] = None

    for tok in _race_stream(endpoints, full_messages, deadline=25):
        yield tok


def _stream_events(messages):
    OPEN = "<thinking>"
    CLOSE = "</thinking>"
    buf = ""
    in_thinking = False
    try:
        for delta in _llm_iter(messages):
            buf += delta
            while True:
                if not in_thinking:
                    idx = buf.find(OPEN)
                    if idx == 0:
                        in_thinking = True
                        buf = buf[len(OPEN):]
                        yield {"type": "thinking_start"}
                        continue
                    elif idx > 0:
                        yield {"type": "response", "content": buf[:idx]}
                        buf = buf[idx:]
                        continue
                    else:
                        if len(buf) > len(OPEN):
                            emit = buf[: -len(OPEN)]
                            if emit:
                                yield {"type": "response", "content": emit}
                            buf = buf[-len(OPEN):]
                        break
                else:
                    idx = buf.find(CLOSE)
                    if idx == 0:
                        in_thinking = False
                        buf = buf[len(CLOSE):]
                        yield {"type": "thinking_end"}
                        continue
                    elif idx > 0:
                        yield {"type": "thinking", "content": buf[:idx]}
                        buf = buf[idx:]
                        continue
                    else:
                        if len(buf) > len(CLOSE):
                            emit = buf[: -len(CLOSE)]
                            if emit:
                                yield {"type": "thinking", "content": emit}
                            buf = buf[-len(CLOSE):]
                        break
        if buf:
            if in_thinking:
                yield {"type": "thinking", "content": buf}
            else:
                yield {"type": "response", "content": buf}
            buf = ""
        if in_thinking:
            yield {"type": "thinking_end"}
    except Exception as e:
        yield {"type": "error", "message": str(e)[:300]}
    yield {"type": "done"}


def _stream_events_keepalive(messages):
    q: "queue.Queue" = queue.Queue()

    def producer():
        try:
            for ev in _stream_events(messages):
                q.put(ev)
        except Exception as e:
            q.put({"type": "error", "message": str(e)[:300]})
        finally:
            q.put(None)

    threading.Thread(target=producer, daemon=True).start()
    while True:
        try:
            ev = q.get(timeout=8)
        except queue.Empty:
            yield {"type": "_keepalive"}
            continue
        if ev is None:
            break
        yield ev


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/health")
def api_health():
    try:
        usage = shutil.disk_usage(DATA_DIR)
        free, total = usage.free, usage.total
    except Exception:
        free, total = 0, 0
    return jsonify({
        "ok": True,
        "ytdlp": HAS_YTDLP,
        "ffmpeg": HAS_FFMPEG,
        "ffprobe": HAS_FFPROBE,
        "disk_free": free,
        "disk_total": total,
        "outdir": load_settings().get("outdir"),
        "jobs": len(JOBS),
        "concurrency": LIMITER.n,
        "active_slots": LIMITER.active,
        "musick": True,
        "musick_model": "Musick AI",
    })


@app.route("/api/system")
def api_system():
    try:
        usage = shutil.disk_usage(DATA_DIR)
        free, total = usage.free, usage.total
    except Exception:
        free, total = 0, 0
    try:
        py = sys.version.split()[0]
    except Exception:
        py = "?"
    return jsonify({
        "python": py,
        "ytdlp": HAS_YTDLP,
        "ffmpeg": HAS_FFMPEG,
        "ffprobe": HAS_FFPROBE,
        "disk_free": free,
        "disk_total": total,
        "data_dir": str(DATA_DIR),
        "outdir": load_settings().get("outdir"),
        "jobs_dir": str(JOBS_DIR),
    })


@app.route("/api/search", methods=["POST"])
def api_search():
    """
    Search YouTube directly preserving YouTube's native song & popularity ranking
    (so queries like 'pisces by Central Cee' return the song, remixes by others,
    covers, and popular videos exactly as YouTube does).
    """
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    source = (data.get("source") or "auto").strip().lower()
    try:
        n = int(data.get("n") or 12)
    except Exception:
        n = 12
    n = max(1, min(40, n))
    if not q:
        return jsonify({"results": []})

    is_url = bool(re.match(r"^https?://", q))

    if not is_url and (source == "tubidy" or not HAS_YTDLP):
        tubidy_results = tubidy_search(q, limit=n)
        if tubidy_results:
            add_search_history(q)
            return jsonify({"results": tubidy_results})
        if not HAS_YTDLP:
            return jsonify({"error": "yt-dlp not installed and Tubidy returned 0 results"}), 500

    if not HAS_YTDLP:
        return jsonify({"error": "yt-dlp not installed"}), 500

    clean_q = re.sub(r"\bby\b", " ", q, flags=re.I).strip() if not is_url else q
    clean_q = re.sub(r"\s+", " ", clean_q)
    search_q = q if is_url else f"https://www.youtube.com/results?search_query={quote_plus(clean_q)}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": n + 6,
        "ignoreerrors": True,
        "noplaylist": True,
        "socket_timeout": 8,
    }
    results = []
    seen_ids = set()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_q, download=False)
        entries = info.get("entries") if info and info.get("_type") == "playlist" else ([info] if info else [])
        for e in (entries or []):
            if not e:
                continue
            ie_key = (e.get("ie_key") or "").lower()
            if "channel" in ie_key or "playlist" in ie_key or "tab" in ie_key:
                continue
            vid = e.get("id") or ""
            title = (e.get("title") or "").strip()
            if not vid or not title or vid in seen_ids:
                continue
            seen_ids.add(vid)
            url = e.get("webpage_url") or e.get("url") or ""
            if not url or not ("youtube.com" in url or "youtu.be" in url):
                url = f"https://www.youtube.com/watch?v={vid}"
            thumb = e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            results.append({
                "id": vid,
                "title": title,
                "url": url,
                "channel": e.get("channel") or e.get("uploader") or e.get("channel_name") or "",
                "duration": e.get("duration"),
                "thumbnail": thumb,
                "view_count": e.get("view_count"),
                "upload_date": e.get("upload_date"),
            })
            if len(results) >= n:
                break
    except Exception as e:
        if not is_url:
            tubidy_results = tubidy_search(q, limit=n)
            if tubidy_results:
                add_search_history(q)
                return jsonify({"results": tubidy_results})
        return jsonify({"error": str(e)[:300]}), 500

    # Keep YouTube's exact native order (song relevance + popularity + remixes)
    if results:
        add_search_history(q)
    return jsonify({"results": results})


@app.route("/api/resolve")
def api_resolve():
    if not HAS_YTDLP:
        return jsonify({"error": "yt-dlp not installed"}), 500
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "missing url"}), 400
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 10}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

    if info.get("_type") == "playlist":
        entries = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            entries.append({
                "title": e.get("title"),
                "url": e.get("webpage_url") or e.get("url"),
                "duration": e.get("duration"),
            })
        return jsonify({
            "type": "playlist",
            "title": info.get("title") or "Playlist",
            "count": len(entries),
            "entries": entries,
            "preview": {"type": "none"},
            "description": (info.get("description") or "")[:5000],
        })

    webpage = info.get("webpage_url") or info.get("original_url") or url
    preview = {"type": "none"}
    if "youtube.com" in webpage or "youtu.be" in webpage:
        vid = info.get("id")
        if vid:
            preview = {
                "type": "iframe",
                "url": f"https://www.youtube-nocookie.com/embed/{vid}?autoplay=1&rel=0",
            }
    else:
        best = None
        for f in info.get("formats") or []:
            if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none"):
                if not best or (f.get("height") or 0) > (best.get("height") or 0):
                    best = f
        if best and best.get("url"):
            preview = {"type": "video", "url": best["url"]}

    formats = []
    for f in info.get("formats") or []:
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "height": f.get("height"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "format_note": f.get("format_note"),
        })

    return jsonify({
        "type": "video",
        "video": {
            "id": info.get("id"),
            "title": info.get("title"),
            "url": webpage,
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "channel": info.get("channel") or info.get("uploader"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
        },
        "formats": formats,
        "preview": preview,
        "description": (info.get("description") or "")[:5000],
        "upload_date": info.get("upload_date"),
        "like_count": info.get("like_count"),
    })


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    query = data.get("query")
    if not url and not query:
        return jsonify({"error": "missing url or query"}), 400
    if url and "tubidy.com" in url:
        m = re.search(r"/watch/([A-Za-z0-9_=-]+)", url)
        if m:
            data["video_id"] = m.group(1)
    elif not HAS_YTDLP:
        return jsonify({"error": "yt-dlp not installed"}), 500
    job = create_job(data)
    return jsonify({"job": public_job(job)})


@app.route("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        jobs = [public_job(j) for j in JOBS.values()]
    return jsonify({
        "jobs": jobs,
        "concurrency": LIMITER.n,
        "active_slots": LIMITER.active,
    })


@app.route("/api/library")
def api_library():
    """Return all completed/saved audio & video files across JOBS_DIR and configured outdir."""
    items = []
    seen_paths = set()
    settings = load_settings()
    outdir = settings.get("outdir")

    # 1. Check active in-memory completed jobs first
    with JOBS_LOCK:
        for j in JOBS.values():
            if j.get("status") == "done" and j.get("file"):
                p = Path(j.get("exported_file") or j["file"])
                if p.exists():
                    seen_paths.add(str(p.resolve()))
                    st = p.stat()
                    items.append({
                        "id": j["id"],
                        "job_id": j["id"],
                        "name": p.name,
                        "title": j.get("title") or p.stem,
                        "channel": j.get("channel") or "",
                        "thumb": j.get("thumb") or "",
                        "ext": p.suffix.lstrip(".").lower(),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "path": str(p),
                        "stream_url": f"/api/file/{j['id']}?dl=0",
                        "download_url": f"/api/file/{j['id']}?dl=1",
                    })

    # 2. Scan JOBS_DIR on disk for persisted downloads from previous sessions
    if JOBS_DIR.exists():
        for job_folder in JOBS_DIR.iterdir():
            if not job_folder.is_dir():
                continue
            jid = job_folder.name
            for f in job_folder.iterdir():
                if f.suffix.lower() not in (".mp3", ".mp4", ".m4a", ".webm", ".opus"):
                    continue
                res = str(f.resolve())
                if res in seen_paths:
                    continue
                seen_paths.add(res)
                st = f.stat()
                items.append({
                    "id": jid,
                    "job_id": jid,
                    "name": f.name,
                    "title": re.sub(r"\s*\[[A-Za-z0-9_-]{6,15}\]$", "", f.stem),
                    "channel": "",
                    "thumb": "",
                    "ext": f.suffix.lstrip(".").lower(),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "path": str(f),
                    "stream_url": f"/api/library/file?path={quote_plus(str(f))}&dl=0",
                    "download_url": f"/api/library/file?path={quote_plus(str(f))}&dl=1",
                })

    # 3. Scan configured outdir on disk
    if outdir and Path(outdir).exists():
        for f in Path(outdir).iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".mp3", ".mp4", ".m4a", ".webm", ".opus"):
                continue
            res = str(f.resolve())
            if res in seen_paths:
                continue
            seen_paths.add(res)
            st = f.stat()
            items.append({
                "id": "out_" + str(abs(hash(res))),
                "job_id": None,
                "name": f.name,
                "title": re.sub(r"\s*\[[A-Za-z0-9_-]{6,15}\]$", "", f.stem),
                "channel": "Saved on device",
                "thumb": "",
                "ext": f.suffix.lstrip(".").lower(),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "path": str(f),
                "stream_url": f"/api/library/file?path={quote_plus(str(f))}&dl=0",
                "download_url": f"/api/library/file?path={quote_plus(str(f))}&dl=1",
            })

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"items": items, "outdir": outdir})


@app.route("/api/library/file")
def api_library_file():
    raw_path = request.args.get("path") or ""
    if not raw_path:
        abort(400)
    p = Path(raw_path).resolve()
    allowed_roots = [DATA_DIR.resolve()]
    outdir = load_settings().get("outdir")
    if outdir:
        allowed_roots.append(Path(outdir).resolve())
    if not any(str(p).startswith(str(root)) for root in allowed_roots):
        abort(403)
    if not p.exists() or not p.is_file():
        abort(404)
    dl = request.args.get("dl") == "1"
    return send_file(str(p), as_attachment=dl, download_name=p.name)


@app.route("/api/library/delete", methods=["POST"])
def api_library_delete():
    data = request.get_json(silent=True) or {}
    raw_path = data.get("path") or ""
    if not raw_path:
        return jsonify({"error": "missing path"}), 400
    p = Path(raw_path).resolve()
    allowed_roots = [DATA_DIR.resolve()]
    outdir = load_settings().get("outdir")
    if outdir:
        allowed_roots.append(Path(outdir).resolve())
    if not any(str(p).startswith(str(root)) for root in allowed_roots):
        abort(403)
    try:
        if p.exists():
            p.unlink()
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"ok": True})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    j["cancelled"] = True
    if j["status"] in ("queued", "starting"):
        j["status"] = "cancelled"
        j["stage"] = "Cancelled"
    return jsonify({"ok": True})


@app.route("/api/retry/<job_id>", methods=["POST"])
def api_retry(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    new = create_job({
        "url": j.get("url"),
        "query": j.get("query"),
        "video_id": j.get("video_id"),
        "mode": j["mode"],
        "height": j.get("height"),
        "abitrate": j.get("abitrate"),
        "format_id": j.get("format_id"),
        "format_acodec": j.get("format_acodec"),
        "playlist": j.get("playlist"),
        "title": j.get("title"),
        "thumb": j.get("thumb"),
        "channel": j.get("channel"),
        "duration": j.get("duration"),
    })
    return jsonify({"job": public_job(new)})


@app.route("/api/remove/<job_id>", methods=["DELETE"])
def api_remove(job_id):
    delete = request.args.get("delete") == "1"
    with JOBS_LOCK:
        j = JOBS.pop(job_id, None)
    if not j:
        return jsonify({"ok": True})
    if delete:
        try:
            shutil.rmtree(j.get("dir"), ignore_errors=True)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/file/<job_id>")
def api_file(job_id):
    j = JOBS.get(job_id)
    if not j or not j.get("file"):
        abort(404)
    p = Path(j["file"]).resolve()
    try:
        p.relative_to(DATA_DIR.resolve())
    except ValueError:
        abort(403)
    if not p.exists():
        abort(404)
    dl = request.args.get("dl") == "1"
    try:
        return send_file(str(p), as_attachment=dl, download_name=p.name)
    except Exception:
        abort(500)


@app.route("/api/open/<job_id>", methods=["POST"])
def api_open(job_id):
    j = JOBS.get(job_id)
    if not j or not j.get("file"):
        return jsonify({"error": "not found"}), 404
    p = j.get("exported_file") or j["file"]
    opener = None
    for cmd in ("termux-open", "xdg-open", "open"):
        if shutil.which(cmd):
            opener = cmd
            break
    if not opener:
        return jsonify({"error": "no system opener available"}), 400
    try:
        import subprocess
        subprocess.Popen([opener, p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/openfolder/<job_id>", methods=["POST"])
def api_openfolder(job_id):
    j = JOBS.get(job_id)
    if not j or not j.get("file"):
        return jsonify({"error": "not found"}), 404
    folder = str(Path(j.get("exported_file") or j["file"]).parent)
    import subprocess
    for cmd in ("termux-open", "xdg-open", "open"):
        if shutil.which(cmd):
            try:
                subprocess.Popen([cmd, folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return jsonify({"ok": True})
            except Exception:
                continue
    return jsonify({"error": "no system opener available"}), 400


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(silent=True) or {}
    cur = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in data:
            cur[k] = data[k]
    save_settings(cur)
    try:
        LIMITER.set(int(cur.get("concurrency", 4)))
    except Exception:
        pass
    return jsonify({"settings": cur})


@app.route("/api/settings/export")
def api_settings_export():
    cur = load_settings()
    return Response(
        json.dumps(cur, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=musick-settings.json"},
    )


@app.route("/api/settings/import", methods=["POST"])
def api_settings_import():
    data = request.get_json(silent=True) or {}
    cur = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in data:
            cur[k] = data[k]
    save_settings(cur)
    try:
        LIMITER.set(int(cur.get("concurrency", 4)))
    except Exception:
        pass
    return jsonify({"ok": True, "settings": cur})


@app.route("/api/history", methods=["GET"])
def api_history_get():
    return jsonify(load_history())


@app.route("/api/history", methods=["DELETE"])
def api_history_delete():
    kind = request.args.get("kind", "all")
    h = load_history()
    if kind in ("all", "searches"):
        h["searches"] = []
    if kind in ("all", "downloads"):
        h["downloads"] = []
    save_history(h)
    return jsonify({"ok": True, "history": h})


@app.route("/api/thumb")
def api_thumb():
    url = request.args.get("url")
    if not url:
        abort(400)
    try:
        r = requests.get(
            url,
            stream=True,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Musick/1.0)",
                "Referer": "https://www.youtube.com/",
            },
        )
        r.raise_for_status()
    except Exception:
        abort(502)
    ctype = (r.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("image/"):
        abort(400)

    def gen():
        sent = 0
        try:
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                sent += len(chunk)
                if sent > 12 * 1024 * 1024:
                    break
                yield chunk
        except Exception:
            return

    resp = Response(stream_with_context(gen()), content_type=ctype)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    settings = load_settings()
    try:
        days = int(settings.get("autoclean_days") or 0)
    except Exception:
        days = 0
    if days <= 0:
        return jsonify({"removed": 0, "reason": "disabled"})
    cutoff = time.time() - days * 86400
    removed = 0
    for entry in list(JOBS_DIR.iterdir()):
        try:
            if not entry.is_dir():
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except Exception:
            continue
    return jsonify({"removed": removed})


@app.route("/api/musick/ping", methods=["POST"])
def api_musick_ping():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip() or None
    endpoints = _resolve_llm(key_override=key)
    errors = []
    for ep in endpoints:
        try:
            headers = {"Content-Type": "application/json"}
            if ep["key"]:
                headers["Authorization"] = f"Bearer {ep['key']}"
            r = requests.post(
                ep["endpoint"],
                json={
                    "model": ep["model"],
                    "messages": [{"role": "user", "content": "Reply with just the word: pong"}],
                    "max_tokens": 8,
                    "stream": False,
                    "temperature": 0.2,
                },
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
            txt = ""
            try:
                txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
            except Exception:
                txt = r.text[:80]
            if txt:
                return jsonify({"ok": True, "provider": ep["name"], "sample": txt[:80]})
            errors.append(ep["name"] + ": empty reply")
        except Exception as e:
            errors.append(ep["name"] + ": " + str(e)[:160])
    return jsonify({"ok": False, "errors": errors}), 502


@app.route("/api/musick/stream", methods=["POST"])
def api_musick_stream():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    clean = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            clean.append({"role": m["role"], "content": m["content"]})

    def generate():
        try:
            for ev in _stream_events_keepalive(clean):
                if ev.get("type") == "_keepalive":
                    yield ": keepalive\n\n"
                else:
                    yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)[:300]})}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache, no-transform"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


@app.route("/<path:path>")
def api_catchall(path):
    silent = {
        "sw.js", "props", "v1/models", "tools", "build.json",
        "favicon.ico", "robots.txt",
    }
    if path in silent:
        return Response("", status=204)
    abort(404)


# ---------------------------------------------------------------------------
# Frontend (single raw-string HTML page with Discover + Library Tabs & Full Player)
# ---------------------------------------------------------------------------
INDEX_HTML = r'''<!doctype html>
<html lang="en" data-theme="dark" data-density="comfortable">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1a1a2e">
<title>Musick · Discover, Download &amp; AI</title>
<style>
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; width: 100%; max-width: 100vw; overflow-x: hidden; }
:root {
  --bg: #1a1a2e;
  --bg-deep: #13131f;
  --surface: #1f1f33;
  --surface-2: #252540;
  --surface-3: #2c2c48;
  --text: #e8e8f0;
  --text-dim: #8a8aa0;
  --text-faint: #5c5c74;
  --accent: #4d6bfe;
  --accent-2: #7c3aed;
  --danger: #e05252;
  --success: #4caf78;
  --warn: #e0b04c;
  --raised: 6px 6px 14px rgba(0,0,0,0.55), -6px -6px 14px rgba(255,255,255,0.03);
  --raised-sm: 3px 3px 8px rgba(0,0,0,0.55), -3px -3px 8px rgba(255,255,255,0.03);
  --inset: inset 4px 4px 10px rgba(0,0,0,0.55), inset -4px -4px 10px rgba(255,255,255,0.03);
  --inset-sm: inset 2px 2px 6px rgba(0,0,0,0.55), inset -2px -2px 6px rgba(255,255,255,0.03);
  --radius: 16px;
  --radius-sm: 10px;
  --radius-lg: 20px;
  --chat-bg: #1e1e2a;
  --chat-bot: #2a2a3a;
  --chat-user: #4d6bfe;
  --chat-header-bg: #23232f;
}
[data-theme="light"] {
  --bg: #e8eaf0;
  --bg-deep: #d8dae5;
  --surface: #eef0f6;
  --surface-2: #f5f7fc;
  --surface-3: #ffffff;
  --text: #1a1d2e;
  --text-dim: #6a6d80;
  --text-faint: #a0a3b5;
  --raised: 6px 6px 14px rgba(170,175,195,0.55), -6px -6px 14px rgba(255,255,255,0.95);
  --raised-sm: 3px 3px 8px rgba(170,175,195,0.55), -3px -3px 8px rgba(255,255,255,0.95);
  --inset: inset 4px 4px 10px rgba(170,175,195,0.55), inset -4px -4px 10px rgba(255,255,255,0.95);
  --inset-sm: inset 2px 2px 6px rgba(170,175,195,0.55), inset -2px -2px 6px rgba(255,255,255,0.95);
  --chat-bg: #ffffff;
  --chat-bot: #f3f4f6;
  --chat-user: #4d6bfe;
  --chat-header-bg: #f8f9fb;
}
[data-density="compact"] { font-size: 13px; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  transition: background .2s, color .2s;
}
button { font-family: inherit; font-size: inherit; cursor: pointer; border: none; background: none; color: inherit; }
input, select, textarea { font-family: inherit; font-size: inherit; color: inherit; }
a { color: var(--accent); text-decoration: none; }
::selection { background: var(--accent); color: #fff; }
.icon {
  width: 18px; height: 18px; flex-shrink: 0;
  fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round;
  vertical-align: middle;
}

@keyframes indeterminate-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
@keyframes search-spin { to { transform: rotate(360deg); } }
@keyframes search-scan {
  0% { left: -40%; }
  100% { left: 100%; }
}
@keyframes confetti-fall {
  0% { transform: translateY(0) rotate(0deg); opacity: 1; }
  100% { transform: translateY(110vh) rotate(720deg); opacity: 0; }
}
@keyframes typing-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}
@keyframes eq-bar {
  0%, 100% { height: 4px; }
  50% { height: 16px; }
}

/* ---------- Top bar & Tabs ---------- */
.topbar { width: 100%; max-width: 430px; margin: 0 auto; flex-wrap: wrap; width: 100%; max-width: 430px; margin: 0 auto; flex-wrap: wrap; max-width: 430px; margin: 0 auto;
  position: sticky; top: 0; z-index: 40;
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 16px;
  background: var(--bg);
  border-bottom: 1px solid rgba(255,255,255,0.05);
  gap: 10px;
}
[data-theme="light"] .topbar { width: 100%; max-width: 430px; margin: 0 auto; flex-wrap: wrap; width: 100%; max-width: 430px; margin: 0 auto; flex-wrap: wrap; max-width: 430px; margin: 0 auto; border-bottom-color: rgba(0,0,0,0.06); }
.global-dl-bar {
  position: absolute; left: 0; right: 0; bottom: 0;
  height: 3px; background: transparent; pointer-events: none;
  overflow: hidden;
}
.global-dl-bar-fill {
  height: 100%; width: 0%;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  transition: width .3s ease;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-icon { width: 24px; height: 24px; color: var(--accent); stroke-width: 2.4; }
.brand-name { font-weight: 800; font-size: 18px; letter-spacing: 0.3px; }

.nav-tabs {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 4px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--inset-sm);
}
.nav-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px; border-radius: 999px;
  font-size: 12.5px; font-weight: 700; color: var(--text-dim);
  transition: all .18s;
}
.nav-tab.active {
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
  box-shadow: 0 3px 10px rgba(77,107,254,0.35);
}
.nav-tab .tab-badge {
  background: rgba(255,255,255,0.2);
  padding: 1px 6px; border-radius: 999px;
  font-size: 10.5px;
}
.nav-tab:not(.active) .tab-badge {
  background: var(--surface-3); color: var(--text);
}

.global-dl-pill {
  display: none; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--raised-sm);
  font-size: 11px; font-weight: 600; color: var(--accent);
  cursor: pointer;
}
.global-dl-pill.show { display: inline-flex; }
.global-dl-pill .pulse-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--accent);
  animation: typing-bounce 1.2s infinite ease-in-out;
}
.topbar-actions { display: flex; align-items: center; gap: 6px; }
.health-pill {
  display: flex; align-items: center; gap: 3px;
  padding: 6px 8px; border-radius: 12px;
  background: var(--surface); box-shadow: var(--raised-sm);
  margin-right: 4px;
}
.health-pill .dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--text-faint);
  transition: background .2s;
}
.health-pill .dot.ok { background: var(--success); box-shadow: 0 0 6px var(--success); }
.health-pill .dot.bad { background: var(--danger); }
.health-pill .dot.warn { background: var(--warn); }
.icon-btn {
  width: 36px; height: 36px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--surface);
  box-shadow: var(--raised-sm);
  color: var(--text);
  transition: transform .15s, box-shadow .15s;
}
.icon-btn:active { box-shadow: var(--inset-sm); transform: scale(0.97); }
.icon-btn:hover { color: var(--accent); }

/* ---------- Main layout ---------- */
.main { max-width: 430px; margin: 0 auto; padding: 14px 12px 140px; }
section { margin-bottom: 26px; }
.raised { background: var(--surface); box-shadow: var(--raised); border-radius: var(--radius); }

/* ---------- Hero / search ---------- */
.hero { padding: 18px; }
.search-row { display: flex; flex-wrap: wrap; gap: 8px; } .search-input-wrap { flex: 1 1 100% !important; width: 100%; } .search-row .btn-primary, .search-row .btn-secondary { flex: 1 1 45%; justify-content: center; }
.search-input-wrap {
  flex: 1; display: flex; align-items: center; gap: 8px;
  padding: 0 10px 0 14px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  box-shadow: var(--inset);
  min-width: 0;
}
.search-input-wrap .search-icon { color: var(--text-dim); }
#searchInput {
  flex: 1; min-width: 0;
  background: transparent; border: none; outline: none;
  padding: 12px 0; color: var(--text);
}
#searchInput::placeholder { color: var(--text-faint); }
.paste-btn {
  width: 30px; height: 30px; border-radius: 8px;
  background: transparent; box-shadow: none;
  color: var(--text-dim);
  display: inline-flex; align-items: center; justify-content: center;
}
.paste-btn:hover { color: var(--accent); background: var(--surface-2); box-shadow: var(--raised-sm); }
.btn-primary {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 18px; border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
  font-weight: 600;
  box-shadow: 4px 4px 12px rgba(77,107,254,0.35), -2px -2px 8px rgba(255,255,255,0.08);
  transition: transform .15s, box-shadow .15s;
  white-space: nowrap;
}
.btn-primary:hover { transform: translateY(-1px); }
.btn-primary:active { transform: translateY(0) scale(0.98); }
.btn-secondary {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 9px 14px; border-radius: var(--radius-sm);
  background: var(--surface); color: var(--text);
  font-weight: 500; box-shadow: var(--raised-sm);
  transition: transform .15s;
  white-space: nowrap;
}
.btn-secondary:hover { color: var(--accent); }
.btn-secondary:active { transform: scale(0.97); box-shadow: var(--inset-sm); }
.btn-text {
  background: none; color: var(--text-dim); font-size: 13px;
  padding: 4px 8px; border-radius: 6px;
}
.btn-text:hover { color: var(--danger); }

.controls-row {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-top: 14px;
}
.segmented {
  display: inline-flex;
  padding: 4px; border-radius: 999px;
  background: var(--surface);
  box-shadow: var(--inset-sm);
  gap: 2px;
}
.seg-btn {
  padding: 6px 14px; border-radius: 999px;
  font-weight: 600; font-size: 12px; color: var(--text-dim);
  transition: all .18s;
}
.seg-btn.active {
  background: var(--surface-3);
  color: var(--text);
  box-shadow: var(--raised-sm);
}
[data-theme="light"] .seg-btn.active { background: #fff; }
.select-control {
  padding: 8px 10px; border-radius: var(--radius-sm);
  background: var(--surface);
  border: none; outline: none;
  box-shadow: var(--inset-sm);
  color: var(--text);
  font-size: 13px;
  appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238a8aa0' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
  background-repeat: no-repeat;
  background-position: right 8px center;
  padding-right: 26px;
}
.chips-row {
  display: flex; flex-wrap: wrap; gap: 6px;
  margin-top: 12px; min-height: 0;
}
.chip {
  padding: 5px 11px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--raised-sm);
  color: var(--text-dim); font-size: 12px;
  transition: all .15s;
}
.chip:hover { color: var(--accent); }
.chip.trending { border: 1px solid rgba(77,107,254,0.22); }

.btn-primary.loading { opacity: 0.88; pointer-events: none; }
.btn-primary.loading span::after {
  content: "";
  display: inline-block;
  width: 11px; height: 11px;
  margin-left: 8px;
  border: 2px solid rgba(255,255,255,0.35);
  border-top-color: #fff;
  border-radius: 50%;
  animation: search-spin 0.75s linear infinite;
  vertical-align: middle;
}
.search-input-wrap.loading { position: relative; overflow: hidden; }
.search-input-wrap.loading::after {
  content: "";
  position: absolute;
  left: -40%; bottom: 0;
  height: 2px; width: 40%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: search-scan 1.1s ease-in-out infinite;
}
.search-input-wrap.loading input { opacity: 0.7; }

/* ---------- Results ---------- */
.section-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 12px; gap: 12px; flex-wrap: wrap;
}
.section-head h2 { font-size: 16px; font-weight: 700; margin: 0; }
.section-head .count-badge { color: var(--text-dim); font-weight: 400; font-size: 13px; margin-left: 4px; }
.section-controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.results-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
}
.result-card {
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius); padding: 10px;
  display: flex; flex-direction: column; gap: 8px;
  transition: transform .15s;
  position: relative;
}
.result-card:hover { transform: translateY(-2px); }
.result-thumb {
  aspect-ratio: 16/9; border-radius: 10px; overflow: hidden;
  position: relative; background: var(--surface-2); cursor: pointer;
}
.result-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.duration-badge {
  position: absolute; bottom: 6px; right: 6px;
  background: rgba(0,0,0,0.8); color: #fff;
  font-size: 11px; padding: 2px 6px; border-radius: 6px;
  font-weight: 600;
}
.result-dl-badge {
  position: absolute; top: 6px; left: 6px;
  background: rgba(19, 19, 31, 0.88);
  color: #fff; font-size: 10px; font-weight: 700;
  padding: 2px 7px; border-radius: 6px;
  backdrop-filter: blur(4px);
  display: none;
}
.result-dl-badge.show { display: inline-block; }
.result-dl-badge.done { background: var(--success); }
.result-dl-badge.error { background: var(--danger); }
.result-title {
  font-size: 13px; font-weight: 600; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; cursor: pointer;
}
.result-title:hover { color: var(--accent); }
.result-meta { font-size: 11px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-actions { display: flex; gap: 6px; margin-top: auto; padding-top: 4px; }
.result-progress-wrap {
  display: none; flex-direction: column; gap: 3px; margin-top: 2px;
}
.result-progress-wrap.show { display: flex; }
.result-progress-info {
  display: flex; justify-content: space-between;
  font-size: 10px; font-weight: 600; color: var(--text-dim);
}
.result-progress {
  height: 5px;
  background: rgba(127,127,127,0.18);
  border-radius: 3px;
  overflow: hidden;
  position: relative;
}
.result-progress-fill {
  height: 100%;
  width: 0%;
  background: var(--accent);
  transition: width .35s ease;
  border-radius: 3px;
}
.result-progress-fill.done { background: var(--success); }
.result-progress-fill.error { background: var(--danger); }
.result-progress-fill.indeterminate {
  width: 100% !important;
  background: linear-gradient(90deg, var(--accent) 0%, var(--accent-2) 50%, var(--accent) 100%);
  background-size: 200% 100%;
  animation: indeterminate-shimmer 1.4s linear infinite;
}
.mini-btn {
  width: 32px; height: 32px; border-radius: 8px;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--surface); box-shadow: var(--raised-sm);
  color: var(--text-dim);
  transition: all .15s;
}
.mini-btn:hover { color: var(--accent); box-shadow: var(--raised); }
.mini-btn:active { box-shadow: var(--inset-sm); transform: scale(0.95); }
.mini-btn.active { color: var(--warn); }

/* ---------- Library & Downloads Tab ---------- */
.library-banner {
  padding: 16px 18px;
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 12px;
  margin-bottom: 18px;
}
.library-path {
  font-size: 12px; color: var(--text-dim);
  background: var(--surface-2);
  padding: 5px 10px; border-radius: 8px;
  font-family: ui-monospace, monospace;
  word-break: break-all;
}
.library-toolbar {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  margin-bottom: 14px;
}
.library-search-box {
  flex: 1; min-width: 200px;
  display: flex; align-items: center; gap: 8px;
  padding: 0 12px; border-radius: var(--radius-sm);
  background: var(--surface); box-shadow: var(--inset-sm);
}
.library-search-box input {
  flex: 1; background: transparent; border: none; outline: none;
  padding: 9px 0; color: var(--text);
}
.library-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}
.lib-card {
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius-sm); padding: 12px;
  display: flex; flex-direction: column; gap: 8px;
  border: 1px solid transparent;
  transition: border-color .18s, transform .15s;
}
.lib-card.playing {
  border-color: var(--accent);
}
.lib-card-top {
  display: flex; align-items: center; gap: 10px;
}
.lib-thumb {
  width: 48px; height: 48px; border-radius: 10px;
  background: linear-gradient(135deg, var(--surface-2), var(--surface-3));
  display: flex; align-items: center; justify-content: center;
  color: var(--accent); overflow: hidden; flex-shrink: 0;
  cursor: pointer; position: relative;
}
.lib-thumb img { width: 100%; height: 100%; object-fit: cover; }
.lib-info { flex: 1; min-width: 0; }
.lib-title {
  font-weight: 700; font-size: 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  cursor: pointer;
}
.lib-title:hover { color: var(--accent); }
.lib-meta { font-size: 11px; color: var(--text-dim); margin-top: 2px; }
.lib-badge {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  padding: 2px 6px; border-radius: 5px;
  background: rgba(77,107,254,0.16); color: var(--accent);
}
.lib-badge.mp4 { background: rgba(124,58,237,0.16); color: #a78bfa; }
.lib-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }

/* ---------- Filter tabs & Queue ---------- */
.filter-tabs {
  display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap;
}
.filter-tab {
  padding: 5px 12px; border-radius: 999px;
  font-size: 12px; font-weight: 600; color: var(--text-dim);
  background: var(--surface); box-shadow: var(--raised-sm);
  transition: all .15s;
}
.filter-tab.active {
  background: var(--accent); color: #fff;
}
.filter-tab .fc { opacity: 0.8; font-size: 11px; margin-left: 3px; }
.jobs-list { display: flex; flex-direction: column; gap: 10px; }
.empty-jobs {
  text-align: center; padding: 32px 16px;
  color: var(--text-faint); font-size: 13px;
}
.job-card {
  padding: 12px 14px;
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius-sm);
  display: flex; flex-direction: column; gap: 8px;
}
.job-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.job-title {
  font-weight: 600; font-size: 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  flex: 1; min-width: 0;
}
.job-pct {
  font-size: 11px; font-weight: 700; color: var(--accent);
  flex-shrink: 0;
}
.job-status {
  font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  text-transform: uppercase; letter-spacing: 0.3px;
  background: var(--surface-2);
  flex-shrink: 0;
}
.job-status.status-queued { color: var(--text-dim); }
.job-status.status-starting { color: var(--warn); }
.job-status.status-downloading { color: var(--accent); }
.job-status.status-processing { color: var(--warn); }
.job-status.status-done { color: var(--success); }
.job-status.status-error { color: var(--danger); }
.job-status.status-cancelled { color: var(--text-dim); }
.job-meta { font-size: 11px; color: var(--text-dim); }
.progress-bar {
  height: 7px; background: var(--surface-2);
  border-radius: 4px; overflow: hidden;
  box-shadow: var(--inset-sm);
}
.progress-fill {
  height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2));
  width: 0%;
  transition: width .35s ease, background .2s;
  border-radius: 4px;
}
.progress-fill.done { background: var(--success); }
.progress-fill.error { background: var(--danger); }
.progress-fill.indeterminate {
  width: 100% !important;
  background: linear-gradient(90deg, var(--accent) 0%, var(--accent-2) 50%, var(--accent) 100%);
  background-size: 200% 100%;
  animation: indeterminate-shimmer 1.4s linear infinite;
}
.job-error {
  font-size: 11px; color: var(--danger);
  padding: 6px 8px; border-radius: 6px;
  background: rgba(224,82,82,0.08);
  word-break: break-word;
}
.job-actions { display: flex; gap: 6px; flex-wrap: wrap; }

/* ---------- Full In-Site Docked Media Player ---------- */
.site-player {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: 84px;
  width: calc(100% - 20px);
  max-width: 410px;
  background: var(--surface);
  box-shadow: 0 14px 40px rgba(0,0,0,0.55);
  border-radius: 16px;
  padding: 10px 14px 12px;
  display: none;
  flex-direction: column;
  gap: 8px;
  z-index: 48;
  border: 1px solid rgba(255,255,255,0.08);
}
.site-player.show { display: flex; }
.sp-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.sp-art {
  width: 44px; height: 44px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; flex-shrink: 0; color: #fff;
}
.sp-art img { width: 100%; height: 100%; object-fit: cover; }
.sp-info { flex: 1; min-width: 0; }
.sp-title { font-size: 13px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sp-sub { font-size: 11px; color: var(--text-dim); display: flex; align-items: center; gap: 8px; }
.eq-bars {
  display: inline-flex; align-items: flex-end; gap: 2px; height: 14px;
}
.eq-bars span {
  width: 3px; height: 4px; background: var(--accent); border-radius: 2px;
}
.site-player.playing .eq-bars span:nth-child(1) { animation: eq-bar 0.6s infinite ease-in-out; }
.site-player.playing .eq-bars span:nth-child(2) { animation: eq-bar 0.6s 0.2s infinite ease-in-out; }
.site-player.playing .eq-bars span:nth-child(3) { animation: eq-bar 0.6s 0.4s infinite ease-in-out; }
.sp-controls { display: flex; align-items: center; justify-content: space-between; width: 100%; gap: 4px; flex-wrap: wrap; }
.sp-btn {
  width: 32px; height: 32px; border-radius: 50%;
  background: var(--surface-2); color: var(--text);
  display: inline-flex; align-items: center; justify-content: center;
  transition: all .15s;
}
.sp-btn:hover { color: var(--accent); }
.sp-btn.primary {
  width: 38px; height: 38px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff;
}
.sp-btn.active { color: var(--accent); background: rgba(77,107,254,0.18); }
.sp-pill-btn {
  padding: 4px 8px; border-radius: 8px;
  background: var(--surface-2); font-size: 11px; font-weight: 700;
  color: var(--text-dim);
}
.sp-pill-btn:hover, .sp-pill-btn.active { color: var(--accent); }
.sp-scrub-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: var(--text-dim);
}
.sp-slider {
  flex: 1; accent-color: var(--accent); height: 4px; cursor: pointer;
}

/* ---------- FAB ---------- */
.fab {
  position: fixed; right: 14px; bottom: 16px;
  width: 58px; height: 58px; border-radius: 50%;
  background: linear-gradient(135deg, #a855f7, #6366f1);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 10px 30px rgba(168,85,247,0.5), 0 4px 12px rgba(99,102,241,0.4);
  z-index: 50;
  transition: transform .2s;
}
.fab:hover { transform: scale(1.06); }
.fab:active { transform: scale(0.96); }
.fab .icon { width: 26px; height: 26px; stroke-width: 2.2; }
.fab[hidden] { display: none; }

/* ---------- Chat panel ---------- */
.chat-panel {
  position: fixed; right: 14px; bottom: 16px;
  width: 430px; max-width: 100vw;
  height: 760px; max-height: calc(100vh - 40px);
  background: var(--chat-bg);
  border-radius: var(--radius-lg);
  box-shadow: 0 30px 80px rgba(0,0,0,0.55), 0 8px 24px rgba(0,0,0,0.35);
  display: flex; flex-direction: column;
  z-index: 200; overflow: hidden;
  border: 1px solid rgba(255,255,255,0.05);
  animation: chatIn .22s ease;
}
@keyframes chatIn { from { opacity: 0; transform: translateY(20px) scale(0.98); } to { opacity: 1; transform: none; } }
[data-theme="light"] .chat-panel { border-color: rgba(0,0,0,0.06); }
.chat-panel[hidden] { display: none; }
.chat-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  background: var(--chat-header-bg);
  border-bottom: 1px solid rgba(255,255,255,0.05);
  flex-shrink: 0;
}
[data-theme="light"] .chat-header { border-bottom-color: rgba(0,0,0,0.06); }
.chat-brand { display: flex; align-items: center; gap: 10px; }
.chat-logo {
  width: 32px; height: 32px; border-radius: 10px;
  background: linear-gradient(135deg, #4d6bfe, #7c3aed);
  display: flex; align-items: center; justify-content: center;
  color: #fff;
}
.chat-logo .icon { width: 18px; height: 18px; }
.chat-title-wrap { display: flex; flex-direction: column; }
.chat-title { font-weight: 700; font-size: 14px; }
.chat-model-badge {
  font-size: 10px; color: var(--text-dim);
  padding: 1px 6px; border-radius: 6px;
  background: rgba(77,107,254,0.12);
  display: inline-block; margin-top: 1px;
  align-self: flex-start;
}
.chat-actions { display: flex; gap: 4px; align-items: center; }
.chat-actions .icon-btn { background: transparent; box-shadow: none; width: 32px; height: 32px; }
.chat-actions .icon-btn:hover { background: rgba(127,127,127,0.1); }

.chat-search-wrap {
  display: none; align-items: center; gap: 6px;
  padding: 8px 14px;
  background: var(--surface);
  border-bottom: 1px solid rgba(127,127,127,0.12);
}
.chat-search-wrap.show { display: flex; }
.chat-search-wrap input {
  flex: 1; background: var(--surface-2); border: none; outline: none;
  padding: 6px 10px; border-radius: 8px; font-size: 12px;
}

.chat-body {
  flex: 1; overflow-y: auto;
  padding: 16px;
  scroll-behavior: smooth;
  position: relative;
}
.chat-empty {
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  text-align: center;
  padding: 40px 16px;
  min-height: 100%;
  gap: 8px;
}
.chat-empty-emoji { font-size: 56px; line-height: 1; margin-bottom: 8px; }
.chat-empty h3 { margin: 0; font-size: 20px; font-weight: 700; }
.chat-empty p { margin: 0 0 20px; color: var(--text-dim); font-size: 13px; max-width: 340px; }
.chat-suggestions { display: flex; flex-direction: column; gap: 8px; width: 100%; max-width: 390px; }
.suggestion-chip {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; border-radius: 12px;
  background: var(--surface); box-shadow: var(--raised-sm);
  text-align: left; font-size: 13px; color: var(--text);
  transition: all .15s;
}
.suggestion-chip:hover { transform: translateY(-1px); color: var(--accent); }
.suggestion-chip .icon { color: var(--accent); }

.chat-messages { display: flex; flex-direction: column; gap: 10px; }
.msg-row { display: flex; gap: 8px; align-items: flex-start; animation: fadeIn .2s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.msg-row.user { flex-direction: row-reverse; }
.msg-avatar {
  width: 26px; height: 26px; border-radius: 8px;
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  color: #fff;
}
.msg-row.bot .msg-avatar { background: linear-gradient(135deg, #4d6bfe, #7c3aed); }
.msg-row.user .msg-avatar { background: #e8eaf0; color: #4d6bfe; }
.msg-avatar .icon { width: 16px; height: 16px; }
.bubble {
  max-width: min(480px, calc(100% - 48px));
  width: fit-content;
  align-self: flex-start;
  padding: 8px 12px 7px;
  border-radius: 14px;
  line-height: 1.45;
  word-wrap: break-word;
  overflow-wrap: break-word;
  min-width: 0;
  font-size: 13.5px;
}
.msg-row.user .bubble { align-self: flex-end; }
.msg-row.bot .bubble { max-width: min(560px, calc(100% - 48px)); }
.msg-row.bot .bubble {
  background: var(--chat-bot);
  border-top-left-radius: 4px;
  color: var(--text);
}
.msg-row.user .bubble {
  background: var(--chat-user);
  color: #fff;
  border-top-right-radius: 4px;
}
.bubble .md p { margin: 0 0 4px; }
.bubble .md p:last-child { margin-bottom: 0; }
.bubble .md ul { margin: 4px 0 8px; padding-left: 20px; }
.bubble .md h1, .bubble .md h2, .bubble .md h3 { margin: 8px 0 6px; font-weight: 700; }
.bubble .md h1 { font-size: 17px; }
.bubble .md h2 { font-size: 16px; }
.bubble .md h3 { font-size: 15px; }
.bubble .md code {
  background: rgba(127,127,127,0.18);
  padding: 1px 5px; border-radius: 4px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px;
}
.bubble .md pre.code-block {
  background: #0d0d14;
  max-width: 100%;
  color: #e8e8f0;
  padding: 10px 12px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 8px 0;
  font-size: 12px;
  position: relative;
}
.bubble .md pre.code-block code { background: none; padding: 0; color: inherit; }
.code-copy-btn {
  position: absolute; top: 6px; right: 6px;
  font-size: 10px; padding: 2px 8px;
  border-radius: 4px; background: rgba(255,255,255,0.1);
  color: #aaa;
  opacity: 0; transition: opacity .15s;
}
pre.code-block:hover .code-copy-btn { opacity: 1; }
.code-copy-btn:hover { color: #fff; background: rgba(255,255,255,0.2); }
.bubble .md a { color: #93aaff; text-decoration: underline; word-break: break-all; }
.msg-row.user .bubble a { color: #fff; }

.thinking {
  margin-bottom: 8px;
  background: rgba(127,127,127,0.12);
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid rgba(127,127,127,0.08);
}
.thinking summary {
  list-style: none;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 12px; color: var(--text-dim);
  display: flex; align-items: center; gap: 6px;
  user-select: none;
}
.thinking summary::-webkit-details-marker { display: none; }
.thinking summary .chev { transition: transform .2s; width: 12px; height: 12px; }
.thinking[open] summary .chev { transform: rotate(90deg); }
.thinking-body {
  padding: 0 12px 10px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px; color: var(--text-dim);
  max-height: 360px; overflow-y: auto;
  white-space: pre-wrap;
  line-height: 1.5;
}
.caret::after {
  content: "\258A";
  display: inline-block;
  margin-left: 1px;
  animation: caret-blink 1s step-end infinite;
}
@keyframes caret-blink { 50% { opacity: 0; } }
.chat-error {
  margin-top: 8px;
  padding: 6px 10px; border-radius: 6px;
  background: rgba(224,82,82,0.14);
  color: #ff8888;
  font-size: 12px;
}
.msg-actions {
  display: flex; gap: 2px; flex-wrap: wrap;
  margin-top: 5px; padding-top: 5px;
  border-top: 1px solid rgba(127,127,127,0.1);
}
.msg-actions button {
  font-size: 10.5px; color: var(--text-dim);
  padding: 2px 7px; border-radius: 6px;
}
.msg-actions button:hover { color: var(--accent); background: rgba(127,127,127,0.12); }
.edit-wrap textarea {
  width: 100%;
  background: rgba(0,0,0,0.15);
  color: inherit;
  border: 1px solid rgba(127,127,127,0.2);
  border-radius: 8px;
  padding: 8px;
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
  min-height: 60px;
  outline: none;
}
.edit-btns { display: flex; gap: 6px; margin-top: 6px; }
.edit-btns button {
  font-size: 11px; padding: 4px 10px; border-radius: 6px;
  background: rgba(127,127,127,0.15); color: inherit;
}
.edit-btns button.primary { background: var(--accent); color: #fff; }

.inline-results {
  display: flex; flex-direction: column; gap: 6px;
  margin-top: 10px;
  border-top: 1px solid rgba(127,127,127,0.12);
  padding-top: 10px;
}
.inline-card {
  display: flex; flex-direction: column; gap: 5px;
  padding: 7px 8px; border-radius: 9px;
  background: rgba(127,127,127,0.09);
}
.inline-card-top {
  display: flex; align-items: center; gap: 8px;
}
.inline-card img { width: 50px; height: 34px; object-fit: cover; border-radius: 5px; flex-shrink: 0; }
.inline-info { flex: 1; min-width: 0; }
.inline-title { font-size: 12px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.inline-channel { font-size: 10px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.inline-actions { display: flex; gap: 3px; flex-shrink: 0; }
.inline-actions .mini-btn { width: 26px; height: 26px; border-radius: 6px; }
.inline-actions .mini-btn .icon { width: 14px; height: 14px; }
.inline-progress-wrap {
  display: none; flex-direction: column; gap: 2px;
}
.inline-progress-wrap.show { display: flex; }
.inline-progress-meta {
  display: flex; justify-content: space-between;
  font-size: 9.5px; font-weight: 600; color: var(--text-dim);
}
.inline-progress-bar {
  height: 4px; border-radius: 2px;
  background: rgba(127,127,127,0.2); overflow: hidden;
}
.inline-progress-fill {
  height: 100%; width: 0%; background: var(--accent);
  transition: width .35s ease;
}
.inline-progress-fill.done { background: var(--success); }
.inline-progress-fill.error { background: var(--danger); }
.inline-progress-fill.indeterminate {
  width: 100% !important;
  background: linear-gradient(90deg, var(--accent) 0%, var(--accent-2) 50%, var(--accent) 100%);
  background-size: 200% 100%;
  animation: indeterminate-shimmer 1.4s linear infinite;
}

.typing-indicator {
  display: none; align-items: center; gap: 4px;
  padding: 10px 14px; margin-top: 8px;
  background: var(--chat-bot); border-radius: 14px;
  width: fit-content;
}
.typing-indicator.show { display: inline-flex; }
.typing-indicator span {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--text-dim);
  animation: typing-bounce 1.2s infinite ease-in-out;
}
.typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
.typing-indicator span:nth-child(3) { animation-delay: 0.4s; }

.scroll-bottom-btn {
  position: sticky; bottom: 8px; left: 50%; transform: translateX(-50%);
  width: 32px; height: 32px; border-radius: 50%;
  background: var(--surface-3); color: var(--text);
  box-shadow: var(--raised-sm);
  display: none; align-items: center; justify-content: center;
  z-index: 10;
}
.scroll-bottom-btn.show { display: flex; }

.chat-input-wrap { padding: 6px 10px 10px; flex-shrink: 0; background: var(--chat-bg); }
.chat-input-inner {
  position: relative;
  display: flex; align-items: flex-end; gap: 4px;
  padding: 3px 4px 3px 10px;
  border-radius: 20px;
  background: var(--surface);
  border: 1.5px solid rgba(127,127,127,0.18);
  transition: border-color .15s;
  min-height: 36px;
}
.chat-input-inner:focus-within { border-color: var(--accent); }
.chat-media-btn {
  width: 28px; height: 28px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-dim);
  flex-shrink: 0;
  align-self: center;
}
.chat-media-btn:hover { color: var(--accent); background: rgba(127,127,127,0.1); }
.chat-media-btn .icon { width: 15px; height: 15px; }
#chatInput {
  flex: 1; min-height: 20px; max-height: 90px;
  background: transparent; border: none; outline: none;
  resize: none;
  padding: 7px 0;
  font-size: 13.5px;
  line-height: 1.35;
  color: var(--text);
}
#chatInput::placeholder { color: var(--text-faint); }
.chat-send {
  width: 30px; height: 30px; border-radius: 50%;
  background: var(--accent); color: #fff;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  transition: all .15s;
  align-self: center;
}
.chat-send:hover { background: #6480ff; }
.chat-send:active { transform: scale(0.94); }
.chat-send .icon { width: 14px; height: 14px; }
.chat-send.stop { background: var(--danger); }
.chat-send.stop:hover { background: #f06a6a; }

@media (max-width: 640px) {
  .chat-panel {
    inset: 0; width: 100%; height: 100%;
    max-width: 100%; max-height: 100%;
    border-radius: 0; bottom: 0; right: 0;
  }
  .site-player {
  }
}

/* ---------- Modals ---------- */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
  z-index: 300;
}
.modal-backdrop[hidden] { display: none; }
.modal {
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--raised);
  width: 100%; max-width: 410px;
  max-height: 90vh;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid rgba(127,127,127,0.1);
  flex-shrink: 0;
}
.modal-head h2 { margin: 0; font-size: 17px; font-weight: 700; }
.modal-body { padding: 18px 20px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
.modal-foot {
  padding: 12px 20px;
  border-top: 1px solid rgba(127,127,127,0.1);
  display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap;
  flex-shrink: 0;
}
.form-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.form-row label { font-size: 13px; color: var(--text-dim); flex-shrink: 0; }
.form-row .select-control, .form-row input[type="text"], .form-row input[type="number"], .form-row input[type="password"] {
  flex: 1; min-width: 0;
  padding: 8px 10px; border-radius: var(--radius-sm);
  background: var(--surface); border: none; outline: none;
  box-shadow: var(--inset-sm);
  font-size: 13px;
  color: var(--text);
}
.form-row input[type="color"] {
  width: 40px; height: 32px; padding: 2px;
  border: none; border-radius: 8px;
  background: var(--surface); box-shadow: var(--inset-sm);
  cursor: pointer;
}
.form-section-title {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--text-faint); font-weight: 700;
  margin: 8px 0 2px;
}
.switch { position: relative; display: inline-block; width: 40px; height: 22px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider {
  position: absolute; inset: 0;
  background: var(--surface-2);
  border-radius: 22px;
  cursor: pointer;
  transition: .2s;
  box-shadow: var(--inset-sm);
}
.switch .slider::before {
  content: ""; position: absolute;
  height: 16px; width: 16px;
  left: 3px; top: 3px;
  background: var(--text-dim);
  border-radius: 50%;
  transition: .2s;
}
.switch input:checked + .slider { background: var(--accent); }
.switch input:checked + .slider::before { transform: translateX(18px); background: #fff; }

.preview-player {
  aspect-ratio: 16/9; background: #000;
  border-radius: 10px; overflow: hidden;
  position: relative;
}
.preview-player iframe,
.preview-player video { width: 100%; height: 100%; display: block; border: none; }
.preview-player .preview-loading,
.preview-player .preview-none,
.preview-player .preview-error {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 13px;
  padding: 20px; text-align: center;
}
.preview-player .preview-loading::before {
  content: "";
  display: inline-block;
  width: 14px; height: 14px;
  margin-right: 8px;
  border: 2px solid rgba(127,127,127,0.3);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: search-spin 0.8s linear infinite;
}
.preview-player .preview-error { color: var(--danger); }
.preview-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.tag {
  font-size: 11px; padding: 3px 8px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--raised-sm);
  color: var(--text-dim);
}
.preview-desc summary {
  cursor: pointer; font-size: 13px; color: var(--text-dim);
  padding: 6px 0; list-style: none; user-select: none;
}
.preview-desc summary::-webkit-details-marker { display: none; }
.preview-desc summary::before { content: "\25B8  "; display: inline-block; }
.preview-desc[open] summary::before { content: "\25BE  "; }
#previewDesc {
  font-size: 12px; color: var(--text-dim);
  max-height: 200px; overflow-y: auto;
  padding: 8px; border-radius: 8px;
  background: var(--surface-2);
  white-space: pre-wrap;
  word-break: break-word;
}

.history-section h3 { font-size: 14px; margin: 0 0 8px; font-weight: 700; }
.history-section { display: flex; flex-direction: column; gap: 8px; }
.history-section-head { display: flex; align-items: center; justify-content: space-between; }
.history-list { display: flex; flex-wrap: wrap; gap: 6px; }
.history-item {
  display: flex; flex-direction: column;
  padding: 8px 10px; border-radius: 8px;
  background: var(--surface-2);
  flex: 1 1 200px;
}
.history-title { font-size: 12px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-meta { font-size: 11px; color: var(--text-dim); }
.muted { color: var(--text-faint); font-size: 12px; padding: 8px 0; }
#batchTextarea {
  width: 100%; min-height: 180px;
  padding: 10px; border-radius: var(--radius-sm);
  background: var(--surface); border: none; outline: none;
  box-shadow: var(--inset-sm);
  font-family: ui-monospace, monospace; font-size: 12px;
  color: var(--text); resize: vertical;
}

/* ---------- Toast ---------- */
.toast-container {
  position: fixed; bottom: 96px; left: 50%; transform: translateX(-50%);
  z-index: 500; display: flex; flex-direction: column; gap: 6px;
  pointer-events: none;
}
.toast {
  background: var(--surface);
  color: var(--text);
  padding: 10px 16px; border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  font-size: 13px; font-weight: 500;
  opacity: 0; transform: translateY(10px);
  transition: opacity .2s, transform .2s;
  max-width: 90vw;
  word-break: break-word;
}
.toast.show { opacity: 1; transform: translateY(0); }
.toast-error { border-left: 3px solid var(--danger); }
.toast-success { border-left: 3px solid var(--success); }

.confetti-piece {
  position: fixed;
  width: 8px; height: 8px;
  border-radius: 2px;
  z-index: 9999;
  pointer-events: none;
  animation: confetti-fall 2.6s ease-out forwards;
}

.concurrency-pill {
  font-size: 11px;
  color: var(--text-dim);
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--surface);
  box-shadow: var(--raised-sm);
  margin-left: 6px;
  font-weight: 600;
  display: inline-block;
  vertical-align: middle;
}
.concurrency-pill.active { color: var(--accent); }
.result-thumb.fallback::after {
  content: "";
  position: absolute; inset: 0;
  background: linear-gradient(135deg, var(--surface-2), var(--surface-3));
  z-index: 0;
}
.result-thumb.fallback .duration-badge { z-index: 2; }

/* ---------- Shortcuts overlay ---------- */
.shortcuts-list { display: flex; flex-direction: column; gap: 6px; }
.shortcut-row {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 13px; padding: 4px 0;
}
.shortcut-row kbd {
  background: var(--surface-2); padding: 2px 8px; border-radius: 6px;
  font-family: ui-monospace, monospace; font-size: 11px;
  box-shadow: var(--raised-sm);
}

@media (max-width: 600px) {
  .main { padding: 14px 12px 140px; }
  .hero { padding: 14px; }
  .search-row { flex-wrap: wrap; }
  .btn-primary { padding: 10px 14px; }
  .controls-row > * { flex: 1 1 auto; }
  .segmented { flex: 1 1 auto; justify-content: center; }
  .results-grid { grid-template-columns: 1fr; }
  .brand-name { display: none; }
  .topbar { width: 100%; max-width: 430px; margin: 0 auto; flex-wrap: wrap; width: 100%; max-width: 430px; margin: 0 auto; flex-wrap: wrap; max-width: 430px; margin: 0 auto; padding: 10px 12px; }
  .health-pill { display: none; }
}
</style>
</head>
<body>

<svg xmlns="http://www.w3.org/2000/svg" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true">
  <symbol id="i-music" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></symbol>
  <symbol id="i-clipboard" viewBox="0 0 24 24"><rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/></symbol>
  <symbol id="i-history" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></symbol>
  <symbol id="i-settings" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></symbol>
  <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></symbol>
  <symbol id="i-eye" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></symbol>
  <symbol id="i-video" viewBox="0 0 24 24"><rect x="2" y="6" width="14" height="12" rx="2"/><path d="m22 8-6 4 6 4V8z"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10"/><path d="M3.51 15a9 9 0 0 0 14.85 3.36L23 14"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-send" viewBox="0 0 24 24"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></symbol>
  <symbol id="i-square" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
  <symbol id="i-brain" viewBox="0 0 24 24"><path d="M12 5a3 3 0 0 0-3-3 3 3 0 0 0-2.6 1.5A3 3 0 0 0 4 6.5 3 3 0 0 0 3 9a3 3 0 0 0 1 2.2A3 3 0 0 0 4 13.5 3 3 0 0 0 6 16a3 3 0 0 0 3 3 3 3 0 0 0 3-2"/><path d="M12 5a3 3 0 0 1 3-3 3 3 0 0 1 2.6 1.5A3 3 0 0 1 20 6.5 3 3 0 0 1 21 9a3 3 0 0 1-1 2.2 3 3 0 0 1 0 2.3 3 3 0 0 1-2 2.5 3 3 0 0 1-3 3 3 3 0 0 1-3-2"/></symbol>
  <symbol id="i-chevron" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
  <symbol id="i-moonstar" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/><path d="M18 6h.01M22 8h.01"/></symbol>
  <symbol id="i-headphones" viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></symbol>
  <symbol id="i-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></symbol>
  <symbol id="i-share" viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.59 13.51 6.83 3.98"/><path d="m15.41 6.51-6.82 3.98"/></symbol>
  <symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></symbol>
  <symbol id="i-keyboard" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h0M10 10h0M14 10h0M18 10h0M6 14h0M18 14h0M10 14h4"/></symbol>
  <symbol id="i-export" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></symbol>
  <symbol id="i-import" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></symbol>
  <symbol id="i-save" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></symbol>
  <symbol id="i-zap" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></symbol>
  <symbol id="i-skip-back" viewBox="0 0 24 24"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5"/></symbol>
  <symbol id="i-skip-fwd" viewBox="0 0 24 24"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></symbol>
  <symbol id="i-repeat" viewBox="0 0 24 24"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></symbol>
  <symbol id="i-shuffle" viewBox="0 0 24 24"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></symbol>
  <symbol id="i-star" viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></symbol>
  <symbol id="i-arrow-down" viewBox="0 0 24 24"><path d="M12 5v14M19 12l-7 7-7-7"/></symbol>
  <symbol id="i-image" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></symbol>
</svg>

<header class="topbar">
  <div class="brand">
    <svg class="icon brand-icon"><use href="#i-music"/></svg>
    <span class="brand-name">Musick</span>
  </div>

  <nav class="nav-tabs" id="mainNavTabs">
    <button class="nav-tab active" data-tab="discover">
      <svg class="icon" style="width:15px;height:15px"><use href="#i-search"/></svg>
      <span>Discover</span>
    </button>
    <button class="nav-tab" data-tab="library">
      <svg class="icon" style="width:15px;height:15px"><use href="#i-headphones"/></svg>
      <span>Downloads</span>
      <span class="tab-badge" id="libraryCountBadge">0</span>
    </button>
  </nav>

  <div class="topbar-actions">
    <div class="global-dl-pill" id="globalDlPill" title="Open Downloads tab">
      <span class="pulse-dot"></span>
      <span id="globalDlText">Downloading…</span>
    </div>
    <div class="health-pill" id="healthPill" title="System status">
      <span class="dot" title="yt-dlp"></span>
      <span class="dot" title="ffmpeg"></span>
      <span class="dot" title="disk"></span>
    </div>
    <button class="icon-btn" id="shortcutsBtn" title="Shortcuts"><svg class="icon"><use href="#i-keyboard"/></svg></button>
    <button class="icon-btn" id="historyBtn" title="History"><svg class="icon"><use href="#i-history"/></svg></button>
    <button class="icon-btn" id="settingsBtn" title="Settings"><svg class="icon"><use href="#i-settings"/></svg></button>
    <button class="icon-btn" id="themeBtn" title="Toggle theme"><svg class="icon" id="themeIcon"><use href="#i-sun"/></svg></button>
  </div>
  <div class="global-dl-bar"><div class="global-dl-bar-fill" id="globalDlBarFill"></div></div>
</header>

<main class="main">
  <!-- ================= TAB 1: DISCOVER ================= -->
  <div id="tabDiscover">
    <section class="hero raised">
      <div class="search-row">
        <div class="search-input-wrap">
          <svg class="icon search-icon"><use href="#i-search"/></svg>
          <input type="text" id="searchInput" placeholder="Search any song, remix, or paste YouTube link…" autocomplete="off" spellcheck="false" list="searchSuggestList">
          <datalist id="searchSuggestList"></datalist>
          <button class="paste-btn" id="pasteBtn" title="Paste from clipboard"><svg class="icon"><use href="#i-clipboard"/></svg></button>
        </div>
        <button class="btn-primary" id="searchBtn">
          <svg class="icon"><use href="#i-search"/></svg>
          <span>Search</span>
        </button>
        <button class="btn-secondary" id="quickDownloadTopBtn" title="Download top YouTube match immediately">
          <svg class="icon"><use href="#i-zap"/></svg>
          <span>Instant DL</span>
        </button>
      </div>

      <div class="controls-row">
        <div class="segmented" id="modeToggle">
          <button class="seg-btn active" data-mode="mp3">MP3</button>
          <button class="seg-btn" data-mode="mp4">MP4</button>
        </div>
        <select id="qualitySelect" class="select-control" hidden>
          <option value="720">720p</option>
          <option value="1080">1080p</option>
          <option value="480">480p</option>
          <option value="best">Best</option>
        </select>
        <select id="bitrateSelect" class="select-control">
          <option value="128">128 kbps</option>
          <option value="192">192 kbps</option>
          <option value="320">320 kbps</option>
        </select>
        <select id="countSelect" class="select-control">
          <option value="8">8 results</option>
          <option value="12" selected>12 results</option>
          <option value="20">20 results</option>
          <option value="30">30 results</option>
        </select>
        <button class="btn-secondary" id="batchBtn">
          <svg class="icon"><use href="#i-plus"/></svg>
          <span>Batch</span>
        </button>
      </div>

      <div class="chips-row" id="chipsRow"></div>
    </section>

    <section class="results" id="resultsSection" hidden>
      <div class="section-head">
        <h2>YouTube Results for <span id="resultsQuery"></span></h2>
        <div class="section-controls">
          <button class="btn-secondary" id="downloadAllResultsBtn" style="padding:6px 10px;font-size:12px" title="Queue top 5 results">
            <svg class="icon" style="width:14px;height:14px"><use href="#i-download"/></svg>
            <span>Queue Top 5</span>
          </button>
          <select id="lengthFilter" class="select-control">
            <option value="all">All lengths</option>
            <option value="short">Short &lt;4m</option>
            <option value="medium">Medium 4–20m</option>
            <option value="long">Long &gt;20m</option>
          </select>
          <select id="sortSelect" class="select-control">
            <option value="relevance">YouTube Order</option>
            <option value="views">Most Views</option>
            <option value="duration">Duration</option>
          </select>
        </div>
      </div>
      <div class="results-grid" id="resultsGrid"></div>
    </section>

    <!-- Active downloads preview on Discover tab -->
    <section id="discoverActiveSection" hidden>
      <div class="section-head">
        <h2>Active Downloads <span id="discoverActiveCount" class="count-badge"></span></h2>
        <button class="btn-text" id="goToDownloadsTabBtn" style="color:var(--accent)">Open Downloads Tab →</button>
      </div>
      <div class="jobs-list" id="discoverActiveList"></div>
    </section>
  </div>

  <!-- ================= TAB 2: DOWNLOADS & LIBRARY ================= -->
  <div id="tabLibrary" hidden>
    <div class="library-banner raised">
      <div>
        <div style="font-weight:700;font-size:15px;margin-bottom:4px">Saved Files &amp; In-Site Media Library</div>
        <div class="library-path" id="libraryOutdirLabel">Loading folder…</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn-primary" id="playAllLibraryBtn">
          <svg class="icon"><use href="#i-play"/></svg>
          <span>Play All</span>
        </button>
        <button class="btn-secondary" id="shuffleAllLibraryBtn">
          <svg class="icon"><use href="#i-shuffle"/></svg>
          <span>Shuffle</span>
        </button>
        <button class="btn-secondary" id="refreshLibraryBtn">
          <svg class="icon"><use href="#i-refresh"/></svg>
          <span>Refresh</span>
        </button>
      </div>
    </div>

    <!-- Saved Files Section -->
    <section>
      <div class="section-head">
        <h2>Downloaded Tracks &amp; Videos <span id="libTotalCount" class="count-badge"></span></h2>
      </div>
      <div class="library-toolbar">
        <div class="library-search-box">
          <svg class="icon" style="color:var(--text-dim)"><use href="#i-search"/></svg>
          <input type="text" id="librarySearchInput" placeholder="Filter downloaded songs…">
        </div>
        <select id="libraryFormatFilter" class="select-control">
          <option value="all">All formats</option>
          <option value="mp3">MP3 Audio</option>
          <option value="mp4">MP4 Video</option>
        </select>
        <select id="librarySortSelect" class="select-control">
          <option value="newest">Newest First</option>
          <option value="name">Name (A–Z)</option>
          <option value="size">Largest Size</option>
        </select>
      </div>
      <div class="library-grid" id="libraryGrid"></div>
      <div class="empty-jobs" id="emptyLibrary" hidden>No downloaded files found yet. Search or ask Musick AI to download a track!</div>
    </section>

    <!-- Download Queue Section -->
    <section class="downloads" id="downloadsSection">
      <div class="section-head">
        <h2>Download Queue <span id="jobsCount" class="count-badge"></span> <span class="concurrency-pill" id="concurrencyPill" style="display:none"></span></h2>
        <div class="section-controls">
          <button class="btn-text" id="clearFinishedBtn" title="Remove finished jobs & delete temp files">Clear finished</button>
          <button class="btn-text" id="clearJobsBtn">Clear list</button>
        </div>
      </div>
      <div class="filter-tabs" id="filterTabs">
        <button class="filter-tab active" data-filter="all">All <span class="fc" data-count="all"></span></button>
        <button class="filter-tab" data-filter="active">Active <span class="fc" data-count="active"></span></button>
        <button class="filter-tab" data-filter="done">Done <span class="fc" data-count="done"></span></button>
        <button class="filter-tab" data-filter="error">Failed <span class="fc" data-count="error"></span></button>
      </div>
      <div class="jobs-list" id="jobsList"></div>
      <div class="empty-jobs" id="emptyJobs">Queue is idle.</div>
    </section>
  </div>
</main>

<button class="fab" id="fabBtn" title="Ask Musick AI">
  <svg class="icon"><use href="#i-brain"/></svg>
</button>

<div class="chat-panel" id="chatPanel" hidden>
  <header class="chat-header">
    <div class="chat-brand">
      <div class="chat-logo"><svg class="icon"><use href="#i-music"/></svg></div>
      <div class="chat-title-wrap">
        <div class="chat-title">Musick AI</div>
        <span class="chat-model-badge">yt-dlp Powered · Smart Discovery</span>
      </div>
    </div>
    <div class="chat-actions">
      <button class="icon-btn" id="chatSearchBtn" title="Search chat"><svg class="icon"><use href="#i-search"/></svg></button>
      <button class="icon-btn" id="chatExportBtn" title="Export chat"><svg class="icon"><use href="#i-export"/></svg></button>
      <button class="icon-btn" id="newChatBtn" title="New chat"><svg class="icon"><use href="#i-plus"/></svg></button>
      <button class="icon-btn" id="closeChatBtn" title="Minimize chat (keeps results)"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
  </header>
  <div class="chat-search-wrap" id="chatSearchWrap">
    <input type="text" id="chatSearchInput" placeholder="Search messages…">
    <button class="btn-text" id="chatSearchClose">×</button>
  </div>
  <div class="chat-body" id="chatBody">
    <div class="chat-empty" id="chatEmpty">
      <div class="chat-empty-emoji">🎧</div>
      <h3>Ask Musick AI anything</h3>
      <p>Search any song, explore remixes, or tell me "download it for me" — I use yt-dlp directly.</p>
      <div class="chat-suggestions">
        <button class="suggestion-chip" data-suggestion="Find Pisces by Central Cee">
          <svg class="icon"><use href="#i-music"/></svg>
          <span>Find Pisces by Central Cee</span>
        </button>
        <button class="suggestion-chip" data-suggestion="Download Blinding Lights by The Weeknd for me">
          <svg class="icon"><use href="#i-download"/></svg>
          <span>Download Blinding Lights by The Weeknd for me</span>
        </button>
        <button class="suggestion-chip" data-suggestion="Search lofi hip hop beats to relax/study to">
          <svg class="icon"><use href="#i-headphones"/></svg>
          <span>Search lofi hip hop beats to relax/study to</span>
        </button>
      </div>
    </div>
    <div class="chat-messages" id="chatMessages"></div>
    <div class="typing-indicator" id="typingIndicator"><span></span><span></span><span></span></div>
    <button class="scroll-bottom-btn" id="scrollBottomBtn" title="Scroll to bottom"><svg class="icon"><use href="#i-arrow-down"/></svg></button>
  </div>
  <div class="chat-input-wrap">
    <div class="chat-input-inner">
      <button class="chat-media-btn" id="chatMediaBtn" type="button" title="Play local audio/video"><svg class="icon"><use href="#i-image"/></svg></button>
      <input type="file" id="chatMediaInput" accept="audio/*,video/*,image/*" hidden>
      <textarea id="chatInput" placeholder="Ask for a song or say 'download it for me'…" rows="1"></textarea>
      <button class="chat-send" id="chatSendBtn" title="Send"><svg class="icon"><use href="#i-send"/></svg></button>
    </div>
  </div>
</div>

<!-- Video Theater Modal for Downloaded MP4s -->
<div class="modal-backdrop" id="videoTheaterModal" hidden>
  <div class="modal" style="max-width:800px">
    <div class="modal-head">
      <h2 id="theaterTitle">Video Player</h2>
      <button class="icon-btn" id="closeTheaterBtn" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <video id="theaterVideo" controls autoplay style="width:100%;border-radius:12px;background:#000;max-height:65vh"></video>
    </div>
  </div>
</div>

<!-- Preview modal -->
<div class="modal-backdrop" id="previewModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2 id="previewTitle">Preview</h2>
      <button class="icon-btn" data-close="previewModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="preview-player" id="previewPlayer"></div>
      <div class="preview-tags" id="previewTags"></div>
      <div class="form-row">
        <label>Mode</label>
        <div class="segmented" id="previewMode">
          <button class="seg-btn active" data-mode="mp3">MP3</button>
          <button class="seg-btn" data-mode="mp4">MP4</button>
        </div>
      </div>
      <div class="form-row" id="previewBitrateRow">
        <label>Bitrate</label>
        <select id="previewBitrate" class="select-control">
          <option value="128">128 kbps</option>
          <option value="192">192 kbps</option>
          <option value="320">320 kbps</option>
        </select>
      </div>
      <div class="form-row" id="previewQualityRow" hidden>
        <label>Quality</label>
        <select id="previewQuality" class="select-control">
          <option value="480">480p</option>
          <option value="720">720p</option>
          <option value="1080">1080p</option>
          <option value="best">Best</option>
        </select>
      </div>
      <div class="form-row" id="previewFormatRow" hidden>
        <label>Exact format</label>
        <select id="previewFormat" class="select-control"></select>
      </div>
      <div class="form-row" id="previewPlaylistRow" hidden>
        <label>Playlist</label>
        <label class="switch">
          <input type="checkbox" id="previewPlaylist">
          <span class="slider"></span>
        </label>
      </div>
      <div class="form-row" id="previewPlaylistLimitRow" hidden>
        <label>Playlist limit</label>
        <input type="number" id="previewPlaylistLimit" min="1" max="200" value="20">
      </div>
      <details class="preview-desc" id="previewDescWrap" hidden>
        <summary>Description</summary>
        <div id="previewDesc"></div>
      </details>
    </div>
    <div class="modal-foot">
      <button class="btn-primary" id="previewDownloadBtn">
        <svg class="icon"><use href="#i-download"/></svg>
        <span>Download</span>
      </button>
    </div>
  </div>
</div>

<!-- Settings modal -->
<div class="modal-backdrop" id="settingsModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>Settings</h2>
      <button class="icon-btn" data-close="settingsModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="form-section-title">Appearance</div>
      <div class="form-row"><label>Theme</label>
        <select id="setTheme" class="select-control"><option value="dark">Dark</option><option value="light">Light</option></select>
      </div>
      <div class="form-row"><label>Follow system theme</label>
        <label class="switch"><input type="checkbox" id="setThemeAuto"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Density</label>
        <select id="setDensity" class="select-control"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select>
      </div>
      <div class="form-row"><label>Accent color</label>
        <input type="color" id="setAccent" value="#4d6bfe">
      </div>

      <div class="form-section-title">Defaults</div>
      <div class="form-row"><label>Default mode</label>
        <select id="setMode" class="select-control"><option value="mp3">MP3</option><option value="mp4">MP4</option></select>
      </div>
      <div class="form-row"><label>Default quality</label>
        <select id="setQuality" class="select-control"><option value="480">480p</option><option value="720">720p</option><option value="1080">1080p</option><option value="best">Best</option></select>
      </div>
      <div class="form-row"><label>Default bitrate</label>
        <select id="setBitrate" class="select-control"><option value="128">128 kbps</option><option value="192">192 kbps</option><option value="320">320 kbps</option></select>
      </div>
      <div class="form-row"><label>Video codec</label>
        <select id="setVideoCodec" class="select-control">
          <option value="any">Any</option><option value="h264">H.264 (best compat)</option>
          <option value="h265">H.265 (smaller)</option><option value="vp9">VP9</option><option value="av1">AV1</option>
        </select>
      </div>

      <div class="form-section-title">Delivery &amp; behavior</div>
      <div class="form-row"><label>Auto-save to browser</label>
        <label class="switch"><input type="checkbox" id="setAutoSave"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Notify on complete</label>
        <label class="switch"><input type="checkbox" id="setNotifyDone"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Auto-retry failed jobs</label>
        <label class="switch"><input type="checkbox" id="setAutoRetry"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Playlist limit</label>
        <input type="number" id="setPlaylistLimit" min="1" max="200" value="20">
      </div>
      <div class="form-row"><label>Filename template</label>
        <input type="text" id="setFilenameTemplate" placeholder="%(title).180B [%(id)s].%(ext)s">
      </div>

      <div class="form-section-title">Advanced</div>
      <div class="form-row"><label>Cookies file</label>
        <input type="text" id="setCookies" placeholder="/path/to/cookies.txt">
      </div>
      <div class="form-row"><label>Proxy URL</label>
        <input type="text" id="setProxy" placeholder="http://host:port">
      </div>
      <div class="form-row"><label>Rate limit</label>
        <input type="text" id="setRateLimit" placeholder="e.g. 2M">
      </div>
      <div class="form-row"><label>Retries</label>
        <input type="number" id="setRetries" min="0" max="20" value="2">
      </div>
      <div class="form-row"><label>Concurrency</label>
        <input type="number" id="setConcurrency" min="1" max="10" value="4">
      </div>
      <div class="form-row"><label>Auto-clean days</label>
        <input type="number" id="setAutoclean" min="0" max="365" value="7">
      </div>
      <div class="form-row"><label>Output folder</label>
        <input type="text" id="setOutdir">
      </div>
      <div class="form-row"><label>Embed thumbnail</label>
        <label class="switch"><input type="checkbox" id="setEmbedThumb"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Embed metadata</label>
        <label class="switch"><input type="checkbox" id="setEmbedMeta"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Download subtitles</label>
        <label class="switch"><input type="checkbox" id="setSubs"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Skip sponsor segments</label>
        <label class="switch"><input type="checkbox" id="setSponsorblock"><span class="slider"></span></label>
      </div>
      <div class="form-row"><label>Normalize audio</label>
        <label class="switch"><input type="checkbox" id="setNormalize"><span class="slider"></span></label>
      </div>

      <div class="form-section-title">Musick AI</div>
      <div class="form-row"><label>API key (optional)</label>
        <input type="password" id="setGeminiKey" placeholder="Leave blank to use built-in">
      </div>
      <div class="form-row">
        <label></label>
        <button class="btn-secondary" id="testAiBtn" style="padding:6px 12px;font-size:12px">
          <svg class="icon" style="width:14px;height:14px"><use href="#i-zap"/></svg>
          <span>Test connection</span>
        </button>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn-secondary" id="exportSettingsBtn"><svg class="icon" style="width:14px;height:14px"><use href="#i-export"/></svg> Export</button>
      <button class="btn-secondary" id="importSettingsBtn"><svg class="icon" style="width:14px;height:14px"><use href="#i-import"/></svg> Import</button>
      <button class="btn-secondary" id="cleanupBtn">Clean old files</button>
      <button class="btn-primary" id="saveSettingsBtn">Save</button>
      <input type="file" id="importFileInput" accept="application/json" hidden>
    </div>
  </div>
</div>

<!-- History modal -->
<div class="modal-backdrop" id="historyModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>History</h2>
      <button class="icon-btn" data-close="historyModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="history-section">
        <div class="history-section-head">
          <h3>Recent searches</h3>
          <button class="btn-text" id="clearSearchesBtn">Clear</button>
        </div>
        <div class="history-list" id="historySearches"></div>
      </div>
      <div class="history-section">
        <div class="history-section-head">
          <h3>Download history</h3>
          <button class="btn-text" id="clearDownloadsBtn">Clear</button>
        </div>
        <div class="history-list" id="historyDownloads"></div>
      </div>
    </div>
  </div>
</div>

<!-- Batch modal -->
<div class="modal-backdrop" id="batchModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>Batch download</h2>
      <button class="icon-btn" data-close="batchModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <p style="margin:0;font-size:12px;color:var(--text-dim)">One URL per line. Will be queued with current mode/quality settings.</p>
      <textarea id="batchTextarea" placeholder="https://youtube.com/watch?v=...&#10;https://youtube.com/watch?v=..."></textarea>
    </div>
    <div class="modal-foot">
      <button class="btn-primary" id="batchQueueBtn">Queue all</button>
    </div>
  </div>
</div>

<!-- Shortcuts modal -->
<div class="modal-backdrop" id="shortcutsModal" hidden>
  <div class="modal" style="max-width:420px">
    <div class="modal-head">
      <h2>Keyboard shortcuts</h2>
      <button class="icon-btn" data-close="shortcutsModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="shortcuts-list">
        <div class="shortcut-row"><span>Focus search</span><kbd>/</kbd></div>
        <div class="shortcut-row"><span>Play / Pause media</span><kbd>Space</kbd></div>
        <div class="shortcut-row"><span>Seek ±5 seconds</span><kbd>← / →</kbd></div>
        <div class="shortcut-row"><span>Send chat message</span><kbd>Enter</kbd></div>
        <div class="shortcut-row"><span>New line in chat</span><kbd>Shift+Enter</kbd></div>
        <div class="shortcut-row"><span>Close modal / chat</span><kbd>Esc</kbd></div>
        <div class="shortcut-row"><span>Show shortcuts</span><kbd>?</kbd></div>
      </div>
    </div>
  </div>
</div>

<!-- Full In-Site Media Player -->
<div class="site-player" id="sitePlayer">
  <div class="sp-top">
    <div class="sp-art" id="spArt"><svg class="icon"><use href="#i-music"/></svg></div>
    <div class="sp-info">
      <div class="sp-title" id="spTitle">—</div>
      <div class="sp-sub">
        <span class="eq-bars"><span></span><span></span><span></span></span>
        <span id="spSub">—</span>
      </div>
    </div>
    <div class="sp-controls">
      <button class="sp-btn" id="spShuffleBtn" title="Shuffle"><svg class="icon" style="width:15px;height:15px"><use href="#i-shuffle"/></svg></button>
      <button class="sp-btn" id="spPrevBtn" title="Previous"><svg class="icon" style="width:15px;height:15px"><use href="#i-skip-back"/></svg></button>
      <button class="sp-btn primary" id="spPlayPause" title="Play/Pause"><svg class="icon" id="spPlayIcon"><use href="#i-play"/></svg></button>
      <button class="sp-btn" id="spNextBtn" title="Next"><svg class="icon" style="width:15px;height:15px"><use href="#i-skip-fwd"/></svg></button>
      <button class="sp-btn" id="spLoopBtn" title="Loop"><svg class="icon" style="width:15px;height:15px"><use href="#i-repeat"/></svg></button>
      <button class="sp-pill-btn" id="spSpeedBtn" title="Playback speed">1x</button>
      <button class="sp-pill-btn" id="spSleepBtn" title="Sleep timer">Sleep</button>
      <button class="sp-btn" id="spClose" title="Close player"><svg class="icon" style="width:15px;height:15px"><use href="#i-x"/></svg></button>
    </div>
  </div>
  <div class="sp-scrub-row">
    <span id="spCurTime">0:00</span>
    <input type="range" class="sp-slider" id="spScrubSlider" min="0" max="100" value="0" step="0.1">
    <span id="spDurTime">0:00</span>
  </div>
</div>

<audio id="globalAudio" preload="metadata"></audio>
<div class="toast-container" id="toastContainer"></div>

<script>
(function() {
"use strict";

const $ = (id) => document.getElementById(id);
const $$ = (sel, root) => (root || document).querySelectorAll(sel);

const state = {
  activeTab: "discover",
  settings: null,
  history: { searches: [], downloads: [] },
  results: [],
  resultsQuery: "",
  jobs: [],
  library: [],
  urlJobMap: {},
  prevStatuses: {},
  pollTimer: null,
  currentPreview: null,
  chat: { open: false, initialized: false, messages: [], streaming: false, abort: null, streamStart: 0 },
  player: {
    queue: [],
    index: -1,
    current: null,
    shuffle: false,
    loop: "all", // "off" | "all" | "one"
    speed: 1,
    sleepEnd: null,
    sleepTimer: null
  }
};

const autoSaveQueue = [];
let autoSaveTimer = null;
let notifPermission = (typeof Notification !== "undefined") ? Notification.permission : "denied";

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }

function formatBytes(n) {
  if (!n) return "0B";
  n = Number(n);
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + u[i];
}
function formatDuration(s) {
  if (!s && s !== 0) return "?";
  s = Math.floor(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return h + ":" + String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
  return m + ":" + String(sec).padStart(2, "0");
}
function formatViews(n) {
  if (!n) return "0 views";
  n = Number(n);
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B views";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M views";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K views";
  return n + " views";
}
function formatDate(d) {
  if (!d) return "";
  d = String(d);
  if (d.length === 8) return d.slice(0, 4) + "-" + d.slice(4, 6) + "-" + d.slice(6, 8);
  return d;
}
function formatRelativeTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 604800) return Math.floor(diff / 86400) + "d ago";
  return d.toLocaleDateString();
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts || {});
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast toast-" + (kind || "info");
  el.textContent = msg;
  $("toastContainer").appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  }, 2800);
}

/* ---------- Navigation Tabs (Discover vs Downloads & Library) ---------- */
function switchTab(tab) {
  state.activeTab = tab;
  $$("#mainNavTabs .nav-tab").forEach(function(b) {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  $("tabDiscover").hidden = tab !== "discover";
  $("tabLibrary").hidden = tab !== "library";
  if (tab === "library") {
    refreshLibrary();
    refreshJobs();
  }
}

$$("#mainNavTabs .nav-tab").forEach(function(b) {
  b.onclick = function() { switchTab(b.dataset.tab); };
});
if ($("goToDownloadsTabBtn")) {
  $("goToDownloadsTabBtn").onclick = function() { switchTab("library"); };
}

/* ---------- Markdown renderer ---------- */
function md(text) {
  if (!text) return "";
  let s = String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const codeBlocks = [];
  s = s.replace(/```[a-zA-Z0-9_-]*\n([\s\S]*?)```/g, function(m, code) {
    codeBlocks.push(code);
    return "\u0000CODE" + (codeBlocks.length - 1) + "\u0000";
  });
  const inlineCodes = [];
  s = s.replace(/`([^`\n]+)`/g, function(m, code) {
    inlineCodes.push(code);
    return "\u0000INL" + (inlineCodes.length - 1) + "\u0000";
  });
  s = s.replace(/^###### (.+)$/gm, "<h3>$1</h3>");
  s = s.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  s = s.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  s = s.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|\n)((?:[*-] .+(?:\n|$))+)/g, function(m, pre, block) {
    const items = block.split("\n").filter(Boolean).map(function(line) {
      return "<li>" + line.replace(/^[*-] /, "") + "</li>";
    }).join("");
    return pre + "<ul>" + items + "</ul>";
  });
  const parts = s.split(/\n{2,}/).map(function(p) {
    p = p.trim();
    if (!p) return "";
    if (/^<(h\d|ul|ol|pre|code|p|div|blockquote)/.test(p)) return p;
    return "<p>" + p.replace(/\n/g, "<br>") + "</p>";
  }).join("");
  s = parts;
  s = s.replace(/\u0000CODE(\d+)\u0000/g, function(m, i) {
    return '<pre class="code-block"><code>' + codeBlocks[+i] + "</code></pre>";
  });
  s = s.replace(/\u0000INL(\d+)\u0000/g, function(m, i) {
    return "<code>" + inlineCodes[+i] + "</code>";
  });
  return s;
}

/* ---------- Settings / theme ---------- */
let systemThemeMQ = null;

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme === "light" ? "light" : "dark");
  const icon = $("themeIcon").querySelector("use");
  if (icon) icon.setAttribute("href", theme === "light" ? "#i-moon" : "#i-sun");
}
function applyAccent(color) {
  if (!color) return;
  document.documentElement.style.setProperty("--accent", color);
}
function applyDensity(d) {
  document.documentElement.setAttribute("data-density", d === "compact" ? "compact" : "comfortable");
}
function applyThemeAuto(on) {
  if (systemThemeMQ) {
    systemThemeMQ.onchange = null;
    systemThemeMQ = null;
  }
  if (on && window.matchMedia) {
    systemThemeMQ = window.matchMedia("(prefers-color-scheme: light)");
    const apply = () => applyTheme(systemThemeMQ.matches ? "light" : "dark");
    apply();
    systemThemeMQ.addEventListener("change", apply);
  }
}

async function loadSettings() {
  try {
    state.settings = await fetchJSON("/api/settings");
    applyTheme(state.settings.theme || "dark");
    applyAccent(state.settings.accent);
    applyDensity(state.settings.density);
    if (state.settings.theme_auto) applyThemeAuto(true);
    setMode(state.settings.mode || "mp3");
    $("qualitySelect").value = state.settings.height || "720";
    $("bitrateSelect").value = state.settings.abitrate || "192";
    if ($("libraryOutdirLabel") && state.settings.outdir) {
      $("libraryOutdirLabel").textContent = "Saved Folder: " + state.settings.outdir;
    }
  } catch (e) { console.error(e); }
}

function setMode(mode) {
  $$("#modeToggle .seg-btn").forEach(function(b) {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  $("qualitySelect").hidden = mode !== "mp4";
  $("bitrateSelect").hidden = mode !== "mp3";
}

function getSegMode(sel) {
  const a = document.querySelector(sel + " .seg-btn.active");
  return a ? a.dataset.mode : "mp3";
}

/* ---------- Health ---------- */
async function refreshHealth() {
  try {
    const h = await fetchJSON("/api/health");
    const pill = $("healthPill");
    const dots = pill.querySelectorAll(".dot");
    if (dots[0]) dots[0].className = "dot " + (h.ytdlp ? "ok" : "bad");
    if (dots[1]) dots[1].className = "dot " + (h.ffmpeg ? "ok" : "bad");
    if (dots[2]) dots[2].className = "dot " + (h.disk_free > 500 * 1024 * 1024 ? "ok" : "warn");
    pill.title = "yt-dlp: " + (h.ytdlp ? "ok" : "missing") +
                 ", ffmpeg: " + (h.ffmpeg ? "ok" : "missing") +
                 ", disk free: " + formatBytes(h.disk_free);
  } catch (e) {}
}

/* ---------- History & Trending Chips ---------- */
async function refreshHistory() {
  try {
    state.history = await fetchJSON("/api/history");
    renderChips();
    updateSuggestions();
  } catch (e) {}
}
function renderChips() {
  const wrap = $("chipsRow");
  const terms = (state.history.searches || []).slice(0, 6);
  const trending = ["Pisces Central Cee", "Lofi hip hop beats", "Afrobeats hits", "Slowed + Reverb"];
  let html = terms.map(function(t) {
    return '<button class="chip" data-term="' + escapeAttr(t) + '">' + escapeHtml(t) + "</button>";
  }).join("");
  if (terms.length < 4) {
    html += trending.slice(0, 4 - terms.length).map(function(t) {
      return '<button class="chip trending" data-term="' + escapeAttr(t) + '">🔥 ' + escapeHtml(t) + "</button>";
    }).join("");
  }
  wrap.innerHTML = html;
  wrap.querySelectorAll(".chip").forEach(function(b) {
    b.onclick = function() { doSearch(b.dataset.term); };
  });
}
function updateSuggestions() {
  const dl = $("searchSuggestList");
  if (!dl) return;
  dl.innerHTML = (state.history.searches || []).map(function(t) {
    return '<option value="' + escapeAttr(t) + '">';
  }).join("");
}

/* ---------- Search ---------- */
async function doSearch(q) {
  q = (q || "").trim();
  if (!q) return;
  const btn = $("searchBtn");
  if (btn && btn.classList.contains("loading")) return;
  $("searchInput").value = q;
  state.resultsQuery = q;
  const wrap = document.querySelector(".search-input-wrap");
  if (btn) btn.classList.add("loading");
  if (wrap) wrap.classList.add("loading");
  try {
    toast("Searching YouTube…");
    const n = parseInt($("countSelect").value, 10) || 12;
    const r = await fetchJSON("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q: q, n: n })
    });
    state.results = r.results || [];
    $("lengthFilter").value = "all";
    $("sortSelect").value = "relevance";
    renderResults();
    $("resultsSection").hidden = state.results.length === 0;
    refreshHistory();
    if (state.results.length) toast("Found " + state.results.length + " YouTube results", "success");
    else toast("No results found", "error");
  } catch (e) {
    toast("Search failed: " + e.message, "error");
  } finally {
    if (btn) btn.classList.remove("loading");
    if (wrap) wrap.classList.remove("loading");
  }
}

$("quickDownloadTopBtn").onclick = async function() {
  const q = ($("searchInput").value || "").trim();
  if (!q) { toast("Enter a song name or URL first", "error"); return; }
  const mode = getSegMode("#modeToggle");
  if (/^https?:\/\//i.test(q)) {
    await queueDownload(q, mode);
  } else {
    await queueDownloadQuery(q, mode);
  }
};

$("downloadAllResultsBtn").onclick = async function() {
  const top = filterAndSort(state.results).slice(0, 5);
  if (!top.length) return;
  const mode = getSegMode("#modeToggle");
  toast("Queuing top " + top.length + " tracks…", "success");
  for (const item of top) {
    await queueDownload(item.url, mode, { title: item.title, thumb: item.thumbnail, channel: item.channel, duration: item.duration });
  }
};

function filterAndSort(list) {
  const len = $("lengthFilter").value;
  const sort = $("sortSelect").value;
  let arr = list.slice();
  if (len !== "all") {
    arr = arr.filter(function(r) {
      const d = r.duration || 0;
      if (len === "short") return d > 0 && d < 240;
      if (len === "medium") return d >= 240 && d <= 1200;
      if (len === "long") return d > 1200;
      return true;
    });
  }
  if (sort === "views") arr.sort(function(a, b) { return (b.view_count || 0) - (a.view_count || 0); });
  else if (sort === "duration") arr.sort(function(a, b) { return (a.duration || 0) - (b.duration || 0); });
  return arr;
}

function renderResults() {
  $("resultsQuery").textContent = state.resultsQuery;
  const filtered = filterAndSort(state.results);
  const grid = $("resultsGrid");
  grid.innerHTML = filtered.map(renderResultCard).join("");
  grid.querySelectorAll("[data-preview]").forEach(function(b) {
    b.onclick = function() { openPreviewByUrl(b.dataset.preview); };
  });
  grid.querySelectorAll("[data-mp3]").forEach(function(b) {
    b.onclick = function() {
      const item = state.results.find(x => x.url === b.dataset.mp3) || {};
      queueDownload(b.dataset.mp3, "mp3", { title: item.title, thumb: item.thumbnail, channel: item.channel, duration: item.duration });
    };
  });
  grid.querySelectorAll("[data-mp4]").forEach(function(b) {
    b.onclick = function() {
      const item = state.results.find(x => x.url === b.dataset.mp4) || {};
      queueDownload(b.dataset.mp4, "mp4", { title: item.title, thumb: item.thumbnail, channel: item.channel, duration: item.duration });
    };
  });
  grid.querySelectorAll("[data-fav]").forEach(function(b) {
    b.onclick = function() {
      const on = toggleFavorite(b.dataset.fav, b.dataset.favTitle, b.dataset.favThumb);
      b.classList.toggle("active", on);
      toast(on ? "Added to favorites" : "Removed from favorites", "success");
    };
  });
  updateAllCardProgress();
}

function renderResultCard(r) {
  const q = (state.settings && state.settings.height) || "720";
  const mp4Label = q === "best" ? "MP4" : q + "p";
  const thumbUrl = r.thumbnail ? ("/api/thumb?url=" + encodeURIComponent(r.thumbnail)) : "";
  return '<div class="result-card" data-result-url="' + escapeAttr(r.url) + '">' +
    '<div class="result-thumb" data-preview="' + escapeAttr(r.url) + '">' +
      '<img src="' + thumbUrl + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display=\'none\';this.parentElement.classList.add(\'fallback\')">' +
      '<span class="result-dl-badge"></span>' +
      '<span class="duration-badge">' + escapeHtml(formatDuration(r.duration)) + "</span>" +
    "</div>" +
    '<div class="result-title" data-preview="' + escapeAttr(r.url) + '">' + escapeHtml(r.title || "") + "</div>" +
    '<div class="result-meta">' + escapeHtml(r.channel || "") + " · " + escapeHtml(formatViews(r.view_count)) + "</div>" +
    '<div class="result-actions">' +
      '<button class="mini-btn" data-preview="' + escapeAttr(r.url) + '" title="Preview"><svg class="icon"><use href="#i-eye"/></svg></button>' +
      '<button class="mini-btn" data-mp3="' + escapeAttr(r.url) + '" title="MP3"><svg class="icon"><use href="#i-music"/></svg></button>' +
      '<button class="mini-btn" data-mp4="' + escapeAttr(r.url) + '" title="' + escapeAttr(mp4Label) + '"><svg class="icon"><use href="#i-video"/></svg></button>' +
      '<button class="mini-btn fav-btn' + (isFavorite(r.url) ? ' active' : '') + '" data-fav="' + escapeAttr(r.url) + '" data-fav-title="' + escapeAttr(r.title || '') + '" data-fav-thumb="' + escapeAttr(r.thumbnail || '') + '" title="Favorite"><svg class="icon"><use href="#i-star"/></svg></button>' +
    "</div>" +
    '<div class="result-progress-wrap">' +
      '<div class="result-progress-info"><span class="rp-stage">Starting</span><span class="rp-pct">0%</span></div>' +
      '<div class="result-progress"><div class="result-progress-fill"></div></div>' +
    '</div>' +
  "</div>";
}

/* ---------- Queue download (URL & Direct Query via yt-dlp) ---------- */
async function queueDownload(url, mode, extra) {
  extra = extra || {};
  const settings = state.settings || {};
  const body = {
    url: url,
    mode: mode || settings.mode || "mp3",
    height: (mode || settings.mode) === "mp4" ? (extra.height || settings.height || "720") : undefined,
    abitrate: (mode || settings.mode) === "mp3" ? (extra.abitrate || settings.abitrate || "192") : undefined,
    format_id: extra.format_id,
    format_acodec: extra.format_acodec,
    playlist: !!extra.playlist,
    playlist_limit: extra.playlist_limit,
    title: extra.title,
    thumb: extra.thumb,
    channel: extra.channel,
    duration: extra.duration
  };
  try {
    const r = await fetchJSON("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (r && r.job && r.job.id && url) {
      state.urlJobMap[url] = r.job.id;
    }
    toast("Downloading via yt-dlp…", "success");
    refreshJobs();
  } catch (e) {
    toast("Download failed: " + e.message, "error");
  }
}

async function queueDownloadQuery(query, mode) {
  const settings = state.settings || {};
  const useMode = mode || settings.mode || "mp3";
  try {
    const r = await fetchJSON("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: query,
        mode: useMode,
        height: useMode === "mp4" ? (settings.height || "720") : undefined,
        abitrate: useMode === "mp3" ? (settings.abitrate || "192") : undefined
      })
    });
    if (r && r.job && r.job.url) {
      state.urlJobMap[r.job.url] = r.job.id;
    }
    toast("yt-dlp downloading: " + query, "success");
    refreshJobs();
  } catch (e) {
    toast("Download failed: " + e.message, "error");
  }
}

/* ---------- Auto-save & notifications ---------- */
function enqueueAutoSave(job) {
  if (state.settings && state.settings.auto_save === false) return;
  autoSaveQueue.push(job);
  if (!autoSaveTimer) autoSaveTimer = setTimeout(drainAutoSave, 300);
}
function drainAutoSave() {
  if (!autoSaveQueue.length) { autoSaveTimer = null; return; }
  const j = autoSaveQueue.shift();
  triggerFileDownload(j.id);
  autoSaveTimer = setTimeout(drainAutoSave, 900);
}
function triggerFileDownload(id) {
  const a = document.createElement("a");
  a.href = "/api/file/" + id + "?dl=1";
  a.download = "";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 200);
}

async function requestNotifPermission() {
  if (typeof Notification === "undefined") return;
  if (Notification.permission === "granted" || Notification.permission === "denied") {
    notifPermission = Notification.permission;
    return;
  }
  try {
    notifPermission = await Notification.requestPermission();
  } catch (e) {}
}
function notify(title, body) {
  if (state.settings && state.settings.notify_done === false) return;
  if (typeof Notification === "undefined") return;
  if (notifPermission !== "granted") return;
  try {
    new Notification(title, { body: body || "", icon: "/favicon.ico", tag: "musick" });
  } catch (e) {}
}

/* ---------- Global & Card Progress Everywhere ---------- */
function updateGlobalProgress() {
  const pill = $("globalDlPill");
  const txt = $("globalDlText");
  const barFill = $("globalDlBarFill");
  const activeJobs = state.jobs.filter(function(j) {
    return ["queued", "starting", "downloading", "processing"].indexOf(j.status) >= 0;
  });
  if (!activeJobs.length) {
    if (pill) pill.classList.remove("show");
    if (barFill) barFill.style.width = "0%";
    return;
  }
  const totalPct = activeJobs.reduce(function(sum, j) { return sum + (j.progress || 0); }, 0);
  const avgPct = Math.round(totalPct / activeJobs.length);
  if (pill) pill.classList.add("show");
  if (txt) {
    txt.textContent = activeJobs.length === 1
      ? ("1 downloading · " + avgPct + "%")
      : (activeJobs.length + " active · " + avgPct + "%");
  }
  if (barFill) barFill.style.width = Math.max(5, avgPct) + "%";
}

function updateAllCardProgress() {
  state.jobs.forEach(function(j) {
    if (j.url) state.urlJobMap[j.url] = j.id;
  });

  document.querySelectorAll(".result-card").forEach(function(card) {
    const url = card.dataset.resultUrl;
    if (!url) return;
    const jobId = state.urlJobMap[url];
    const job = jobId ? state.jobs.find(function(j) { return j.id === jobId; }) : null;
    const wrap = card.querySelector(".result-progress-wrap");
    const fill = card.querySelector(".result-progress-fill");
    const stageEl = card.querySelector(".rp-stage");
    const pctEl = card.querySelector(".rp-pct");
    const badge = card.querySelector(".result-dl-badge");
    if (!wrap || !fill) return;
    if (!job || job.status === "cancelled") {
      wrap.classList.remove("show");
      if (badge) badge.classList.remove("show");
      return;
    }
    wrap.classList.add("show");
    const prog = job.status === "done" ? 100 : Math.round(job.progress || 0);
    let cls = "result-progress-fill";
    if (job.status === "done") cls += " done";
    else if (job.status === "error") cls += " error";
    else if (job.status === "queued" || job.status === "starting" || job.status === "processing") cls += " indeterminate";
    fill.className = cls;
    fill.style.width = prog + "%";
    if (stageEl) {
      stageEl.textContent = job.status === "done" ? "Saved in Downloads" : (job.status === "error" ? "Failed" : (job.speed || job.stage || job.status));
    }
    if (pctEl) pctEl.textContent = prog + "%";
    if (badge) {
      badge.classList.add("show");
      badge.classList.toggle("done", job.status === "done");
      badge.classList.toggle("error", job.status === "error");
      badge.textContent = job.status === "done" ? "✓ Saved" : (job.status === "error" ? "Error" : prog + "%");
    }
  });

  document.querySelectorAll(".inline-card").forEach(function(card) {
    const url = card.dataset.inlineUrl;
    if (!url) return;
    const jobId = state.urlJobMap[url];
    const job = jobId ? state.jobs.find(function(j) { return j.id === jobId; }) : null;
    const wrap = card.querySelector(".inline-progress-wrap");
    const fill = card.querySelector(".inline-progress-fill");
    const metaStage = card.querySelector(".ip-stage");
    const metaPct = card.querySelector(".ip-pct");
    const playBtn = card.querySelector("[data-inline-play]");
    if (!wrap || !fill) return;
    if (!job || job.status === "cancelled") {
      wrap.classList.remove("show");
      if (playBtn) playBtn.hidden = true;
      return;
    }
    wrap.classList.add("show");
    const prog = job.status === "done" ? 100 : Math.round(job.progress || 0);
    let cls = "inline-progress-fill";
    if (job.status === "done") cls += " done";
    else if (job.status === "error") cls += " error";
    else if (job.status === "queued" || job.status === "starting" || job.status === "processing") cls += " indeterminate";
    fill.className = cls;
    fill.style.width = prog + "%";
    if (metaStage) {
      metaStage.textContent = job.status === "done" ? "Ready to play" : (job.status === "error" ? "Failed" : (job.speed || job.stage || job.status));
    }
    if (metaPct) metaPct.textContent = prog + "%";
    if (playBtn) {
      playBtn.hidden = !(job.status === "done" && job.file);
      playBtn.dataset.inlinePlay = job.id;
    }
  });
}

/* ---------- Library & In-Site Media Manager ---------- */
async function refreshLibrary() {
  try {
    const r = await fetchJSON("/api/library");
    state.library = r.items || [];
    if ($("libraryCountBadge")) {
      $("libraryCountBadge").textContent = state.library.length;
    }
    if ($("libTotalCount")) {
      $("libTotalCount").textContent = "(" + state.library.length + " files)";
    }
    if ($("libraryOutdirLabel") && r.outdir) {
      $("libraryOutdirLabel").textContent = "Saved Folder: " + r.outdir;
    }
    renderLibrary();
  } catch (e) {}
}

function getFilteredLibrary() {
  const q = ($("librarySearchInput") ? $("librarySearchInput").value : "").toLowerCase().trim();
  const fmt = $("libraryFormatFilter") ? $("libraryFormatFilter").value : "all";
  const sort = $("librarySortSelect") ? $("librarySortSelect").value : "newest";
  let arr = state.library.slice();
  if (fmt !== "all") {
    arr = arr.filter(function(item) { return item.ext === fmt; });
  }
  if (q) {
    arr = arr.filter(function(item) {
      return (item.title || "").toLowerCase().indexOf(q) >= 0 ||
             (item.channel || "").toLowerCase().indexOf(q) >= 0;
    });
  }
  if (sort === "name") {
    arr.sort(function(a, b) { return (a.title || "").localeCompare(b.title || ""); });
  } else if (sort === "size") {
    arr.sort(function(a, b) { return (b.size || 0) - (a.size || 0); });
  } else {
    arr.sort(function(a, b) { return (b.mtime || 0) - (a.mtime || 0); });
  }
  return arr;
}

function renderLibrary() {
  const grid = $("libraryGrid");
  const empty = $("emptyLibrary");
  if (!grid) return;
  const items = getFilteredLibrary();
  if (!items.length) {
    grid.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  const curId = state.player.current ? state.player.current.id : null;
  grid.innerHTML = items.map(function(item) {
    const isPlaying = curId === item.id;
    const thumbHtml = item.thumb
      ? '<img src="/api/thumb?url=' + encodeURIComponent(item.thumb) + '" alt="">'
      : '<svg class="icon"><use href="' + (item.ext === "mp4" ? "#i-video" : "#i-music") + '"/></svg>';
    return '<div class="lib-card' + (isPlaying ? ' playing' : '') + '">' +
      '<div class="lib-card-top">' +
        '<div class="lib-thumb" data-lib-play="' + escapeAttr(item.id) + '">' + thumbHtml + '</div>' +
        '<div class="lib-info">' +
          '<div class="lib-title" data-lib-play="' + escapeAttr(item.id) + '" title="' + escapeAttr(item.title) + '">' + escapeHtml(item.title) + '</div>' +
          '<div class="lib-meta">' +
            '<span class="lib-badge ' + escapeAttr(item.ext) + '">' + escapeHtml(item.ext) + '</span> · ' +
            escapeHtml(formatBytes(item.size)) + (item.channel ? ' · ' + escapeHtml(item.channel) : '') +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="lib-actions">' +
        '<button class="mini-btn" data-lib-play="' + escapeAttr(item.id) + '" title="Play in site"><svg class="icon"><use href="#i-play"/></svg></button>' +
        '<a class="mini-btn" href="' + escapeAttr(item.download_url) + '" download title="Save to device"><svg class="icon"><use href="#i-save"/></svg></a>' +
        '<button class="mini-btn" data-lib-copy="' + escapeAttr(item.path) + '" title="Copy file path"><svg class="icon"><use href="#i-copy"/></svg></button>' +
        '<button class="mini-btn" data-lib-del="' + escapeAttr(item.path) + '" title="Delete file"><svg class="icon"><use href="#i-trash"/></svg></button>' +
      '</div>' +
    '</div>';
  }).join("");

  grid.querySelectorAll("[data-lib-play]").forEach(function(el) {
    el.onclick = function() {
      const list = getFilteredLibrary();
      const idx = list.findIndex(function(x) { return x.id === el.dataset.libPlay; });
      if (idx >= 0) playLibraryQueue(list, idx);
    };
  });
  grid.querySelectorAll("[data-lib-copy]").forEach(function(el) {
    el.onclick = async function() {
      try {
        await navigator.clipboard.writeText(el.dataset.libCopy);
        toast("File path copied", "success");
      } catch (e) { toast("Copy failed", "error"); }
    };
  });
  grid.querySelectorAll("[data-lib-del]").forEach(function(el) {
    el.onclick = async function() {
      if (el.dataset.armed !== "1") {
        el.dataset.armed = "1";
        el.style.color = "var(--danger)";
        toast("Click trash again to confirm delete");
        setTimeout(() => { el.dataset.armed = "0"; el.style.color = ""; }, 2500);
        return;
      }
      try {
        await fetchJSON("/api/library/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: el.dataset.libDel })
        });
        toast("File deleted", "success");
        refreshLibrary();
      } catch (e) { toast("Delete failed: " + e.message, "error"); }
    };
  });
}

if ($("librarySearchInput")) $("librarySearchInput").oninput = renderLibrary;
if ($("libraryFormatFilter")) $("libraryFormatFilter").onchange = renderLibrary;
if ($("librarySortSelect")) $("librarySortSelect").onchange = renderLibrary;
if ($("refreshLibraryBtn")) $("refreshLibraryBtn").onclick = function() { refreshLibrary(); toast("Library refreshed"); };
if ($("playAllLibraryBtn")) $("playAllLibraryBtn").onclick = function() {
  const list = getFilteredLibrary();
  if (!list.length) { toast("No tracks to play", "error"); return; }
  state.player.shuffle = false;
  updatePlayerButtons();
  playLibraryQueue(list, 0);
};
if ($("shuffleAllLibraryBtn")) $("shuffleAllLibraryBtn").onclick = function() {
  const list = getFilteredLibrary();
  if (!list.length) { toast("No tracks to play", "error"); return; }
  state.player.shuffle = true;
  updatePlayerButtons();
  playLibraryQueue(list, Math.floor(Math.random() * list.length));
};

/* ---------- Jobs ---------- */
async function refreshJobs() {
  try {
    const r = await fetchJSON("/api/jobs");
    state.jobs = r.jobs || [];
    handleJobTransitions();
    renderJobs();
    renderDiscoverActiveJobs();
    updateGlobalProgress();
    updateAllCardProgress();
    const pill = $("concurrencyPill");
    if (pill && r.concurrency) {
      const act = r.active_slots || 0;
      pill.style.display = "inline-block";
      pill.classList.toggle("active", act > 0);
      pill.textContent = act + "/" + r.concurrency + " slots";
    }
    const active = state.jobs.some(function(j) {
      return ["queued", "starting", "downloading", "processing"].indexOf(j.status) >= 0;
    });
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
    if (active) state.pollTimer = setTimeout(refreshJobs, 450);
  } catch (e) {
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  }
}

function handleJobTransitions() {
  let anyCompleted = false;
  state.jobs.forEach(function(j) {
    const prev = state.prevStatuses[j.id];
    if (prev && prev !== j.status) {
      if (j.status === "done" && !j._autoSaved) {
        j._autoSaved = true;
        anyCompleted = true;
        enqueueAutoSave(j);
        notify("Download complete", j.title || j.url);
        try { confettiBurst(); } catch (e) {}
        try { playChime(); } catch (e) {}
        try { haptic(); } catch (e) {}
      }
      if (j.status === "error") {
        notify("Download failed", (j.title || j.url) + " — " + (j.error || ""));
      }
    }
    state.prevStatuses[j.id] = j.status;
  });
  if (anyCompleted) {
    refreshLibrary();
  }
}

function renderDiscoverActiveJobs() {
  const sec = $("discoverActiveSection");
  const list = $("discoverActiveList");
  const cnt = $("discoverActiveCount");
  if (!sec || !list) return;
  const active = state.jobs.filter(function(j) {
    return ["queued", "starting", "downloading", "processing"].indexOf(j.status) >= 0;
  });
  if (!active.length) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;
  if (cnt) cnt.textContent = "(" + active.length + ")";
  list.innerHTML = active.map(renderJobCard).join("");
  wireJobCardButtons(list);
}

function wireJobCardButtons(container) {
  container.querySelectorAll("[data-job-cancel]").forEach(function(b) {
    b.onclick = function() { cancelJob(b.dataset.jobCancel); };
  });
  container.querySelectorAll("[data-job-retry]").forEach(function(b) {
    b.onclick = function() { retryJob(b.dataset.jobRetry); };
  });
  container.querySelectorAll("[data-job-remove]").forEach(function(b) {
    b.onclick = function() { removeJob(b.dataset.jobRemove, b.dataset.del || "0"); };
  });
  container.querySelectorAll("[data-job-save]").forEach(function(b) {
    b.onclick = function() { triggerFileDownload(b.dataset.jobSave); toast("Saving to device…"); };
  });
  container.querySelectorAll("[data-job-play]").forEach(function(b) {
    b.onclick = function() {
      const job = state.jobs.find(function(j) { return j.id === b.dataset.jobPlay; });
      if (job) playJobMedia(job);
    };
  });
  container.querySelectorAll("[data-job-copy]").forEach(function(b) {
    b.onclick = async function() {
      const j = state.jobs.find(x => x.id === b.dataset.jobCopy);
      const path = j && (j.exported_file || j.file);
      if (path) {
        try { await navigator.clipboard.writeText(path); toast("Path copied", "success"); }
        catch (e) { toast("Copy failed", "error"); }
      }
    };
  });
  container.querySelectorAll("[data-job-share]").forEach(function(b) {
    b.onclick = async function() {
      const link = location.origin + "/api/file/" + b.dataset.jobShare + "?dl=1";
      if (navigator.share) {
        try { await navigator.share({ title: "Musick download", url: link }); return; } catch (e) {}
      }
      try { await navigator.clipboard.writeText(link); toast("Share link copied", "success"); }
      catch (e) { toast("Share failed", "error"); }
    };
  });
  container.querySelectorAll("[data-job-open]").forEach(function(b) {
    b.onclick = function() {
      fetchJSON("/api/open/" + b.dataset.jobOpen, { method: "POST" })
        .then(() => toast("Opened"))
        .catch(err => toast("Open failed: " + err.message, "error"));
    };
  });
  container.querySelectorAll("[data-job-folder]").forEach(function(b) {
    b.onclick = function() {
      fetchJSON("/api/openfolder/" + b.dataset.jobFolder, { method: "POST" })
        .then(() => toast("Opened folder"))
        .catch(err => toast("Open failed: " + err.message, "error"));
    };
  });
}

function renderJobs() {
  const list = $("jobsList");
  const empty = $("emptyJobs");
  if (!state.jobs.length) {
    list.innerHTML = "";
    empty.hidden = false;
    $("jobsCount").textContent = "";
    applyJobFilter();
    return;
  }
  empty.hidden = true;
  $("jobsCount").textContent = "(" + state.jobs.length + ")";
  const sorted = state.jobs.slice().sort(function(a, b) {
    return (b.created || 0) - (a.created || 0);
  });
  list.innerHTML = sorted.map(renderJobCard).join("");
  applyJobFilter();
  wireJobCardButtons(list);
}

function renderJobCard(j) {
  const meta = [];
  if (j.stage && j.status !== "done") meta.push(j.stage);
  if (j.mode) meta.push(j.mode.toUpperCase());
  if (j.mode === "mp3" && j.abitrate) meta.push(j.abitrate + " kbps");
  if (j.mode === "mp4" && j.height) meta.push(j.height + "p");
  if (j.downloaded && j.total) meta.push(formatBytes(j.downloaded) + " / " + formatBytes(j.total));
  else if (j.total) meta.push(formatBytes(j.total));
  if (j.speed) meta.push(j.speed);
  if (j.eta) meta.push("ETA " + j.eta);
  if (j.retried) meta.push("retried");

  let actions = "";
  const active = ["queued", "starting", "downloading", "processing"].indexOf(j.status) >= 0;
  if (active) {
    actions += '<button class="mini-btn" data-job-cancel="' + j.id + '" title="Cancel"><svg class="icon"><use href="#i-stop"/></svg></button>';
  }
  if (j.status === "done" && j.file) {
    actions += '<button class="mini-btn" data-job-play="' + j.id + '" title="Play in site"><svg class="icon"><use href="#i-play"/></svg></button>';
    actions += '<button class="mini-btn" data-job-save="' + j.id + '" title="Save to device"><svg class="icon"><use href="#i-save"/></svg></button>';
    actions += '<button class="mini-btn" data-job-open="' + j.id + '" title="Open in system app"><svg class="icon"><use href="#i-download"/></svg></button>';
    actions += '<button class="mini-btn" data-job-folder="' + j.id + '" title="Open folder"><svg class="icon"><use href="#i-folder"/></svg></button>';
    actions += '<button class="mini-btn" data-job-copy="' + j.id + '" title="Copy path"><svg class="icon"><use href="#i-copy"/></svg></button>';
    actions += '<button class="mini-btn" data-job-share="' + j.id + '" title="Share"><svg class="icon"><use href="#i-share"/></svg></button>';
  }
  if (j.status === "error" || j.status === "cancelled") {
    actions += '<button class="mini-btn" data-job-retry="' + j.id + '" title="Retry"><svg class="icon"><use href="#i-refresh"/></svg></button>';
  }
  actions += '<button class="mini-btn" data-job-remove="' + j.id + '" data-del="0" title="Remove"><svg class="icon"><use href="#i-x"/></svg></button>';

  let fillClass = "";
  if (j.status === "done") fillClass = "done";
  else if (j.status === "error") fillClass = "error";
  else if (j.status === "queued" || j.status === "starting" || j.status === "processing") fillClass = "indeterminate";
  const progress = j.status === "done" ? 100 : Math.max(0, Math.min(100, j.progress || 0));

  return '<div class="job-card">' +
    '<div class="job-head">' +
      '<div class="job-title" title="' + escapeAttr(j.title || j.url || "") + '">' + escapeHtml(j.title || j.url || "Untitled") + "</div>" +
      '<span class="job-pct">' + Math.round(progress) + '%</span>' +
      '<div class="job-status status-' + escapeAttr(j.status) + '">' + escapeHtml(j.status) + "</div>" +
    "</div>" +
    '<div class="job-meta">' + escapeHtml(meta.join(" · ")) + "</div>" +
    '<div class="progress-bar"><div class="progress-fill ' + fillClass + '" style="width:' + progress + '%"></div></div>' +
    (j.error ? '<div class="job-error">' + escapeHtml(j.error) + "</div>" : "") +
    '<div class="job-actions">' + actions + "</div>" +
  "</div>";
}

async function cancelJob(id) {
  try { await fetchJSON("/api/cancel/" + id, { method: "POST" }); refreshJobs(); }
  catch (e) { toast(e.message, "error"); }
}
async function retryJob(id) {
  try { await fetchJSON("/api/retry/" + id, { method: "POST" }); refreshJobs(); }
  catch (e) { toast(e.message, "error"); }
}
async function removeJob(id, del) {
  try { await fetchJSON("/api/remove/" + id + "?delete=" + (del || "0"), { method: "DELETE" }); refreshJobs(); }
  catch (e) { toast(e.message, "error"); }
}

$("clearJobsBtn").onclick = async function() {
  const finished = state.jobs.filter(function(j) {
    return ["done", "error", "cancelled"].indexOf(j.status) >= 0;
  });
  for (const j of finished) {
    try { await fetchJSON("/api/remove/" + j.id + "?delete=0", { method: "DELETE" }); } catch (e) {}
  }
  refreshJobs();
};
$("clearFinishedBtn").onclick = async function() {
  const finished = state.jobs.filter(function(j) {
    return ["done", "error", "cancelled"].indexOf(j.status) >= 0;
  });
  for (const j of finished) {
    try { await fetchJSON("/api/remove/" + j.id + "?delete=1", { method: "DELETE" }); } catch (e) {}
  }
  refreshJobs();
};

/* ---------- Full In-Site Audio & Video Player ---------- */
function playJobMedia(job) {
  if (!job || !job.id) return;
  const item = {
    id: job.id,
    title: job.title || "Untitled",
    channel: job.channel || "",
    thumb: job.thumb || "",
    ext: (job.mode || "mp3").toLowerCase(),
    stream_url: "/api/file/" + job.id + "?dl=0"
  };
  playLibraryQueue([item], 0);
}

function playLibraryQueue(list, index) {
  if (!list || !list.length || index < 0 || index >= list.length) return;
  state.player.queue = list;
  state.player.index = index;
  const track = list[index];
  state.player.current = track;

  // If video (MP4), open in-site video theater modal
  if (track.ext === "mp4") {
    const audio = $("globalAudio");
    if (audio) audio.pause();
    $("sitePlayer").classList.remove("show");
    $("theaterTitle").textContent = track.title || "Video Player";
    $("theaterVideo").src = track.stream_url;
    $("videoTheaterModal").hidden = false;
    renderLibrary();
    return;
  }

  const audio = $("globalAudio");
  const sp = $("sitePlayer");
  audio.src = track.stream_url;
  audio.playbackRate = state.player.speed;
  audio.play().catch(function() {});

  sp.classList.add("show", "playing");
  $("spArt").innerHTML = track.thumb
    ? '<img src="/api/thumb?url=' + encodeURIComponent(track.thumb) + '" alt="">'
    : '<svg class="icon"><use href="#i-music"/></svg>';
  $("spTitle").textContent = track.title || "Unknown";
  $("spSub").textContent = (track.ext || "MP3").toUpperCase() + (track.channel ? " · " + track.channel : "");
  $("spPlayIcon").querySelector("use").setAttribute("href", "#i-pause");
  renderLibrary();

  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: track.title || "Musick",
        artist: track.channel || "Musick",
        album: "Musick Library",
        artwork: track.thumb ? [{ src: "/api/thumb?url=" + encodeURIComponent(track.thumb), sizes: "320x180", type: "image/jpeg" }] : []
      });
      navigator.mediaSession.setActionHandler("play", function() { audio.play(); });
      navigator.mediaSession.setActionHandler("pause", function() { audio.pause(); });
      navigator.mediaSession.setActionHandler("previoustrack", playPrevTrack);
      navigator.mediaSession.setActionHandler("nexttrack", playNextTrack);
    } catch (e) {}
  }
}

function playNextTrack() {
  const q = state.player.queue;
  if (!q.length) return;
  if (state.player.loop === "one") {
    playLibraryQueue(q, state.player.index);
    return;
  }
  let nextIdx = state.player.index + 1;
  if (state.player.shuffle && q.length > 1) {
    nextIdx = Math.floor(Math.random() * q.length);
  }
  if (nextIdx >= q.length) {
    if (state.player.loop === "all") nextIdx = 0;
    else return;
  }
  playLibraryQueue(q, nextIdx);
}

function playPrevTrack() {
  const audio = $("globalAudio");
  if (audio && audio.currentTime > 3) {
    audio.currentTime = 0;
    return;
  }
  const q = state.player.queue;
  if (!q.length) return;
  let prevIdx = state.player.index - 1;
  if (prevIdx < 0) prevIdx = q.length - 1;
  playLibraryQueue(q, prevIdx);
}

function updatePlayerButtons() {
  if ($("spShuffleBtn")) $("spShuffleBtn").classList.toggle("active", state.player.shuffle);
  if ($("spLoopBtn")) {
    $("spLoopBtn").classList.toggle("active", state.player.loop !== "off");
    $("spLoopBtn").title = "Loop: " + state.player.loop;
  }
  if ($("spSpeedBtn")) $("spSpeedBtn").textContent = state.player.speed + "x";
}

function wireSitePlayer() {
  const audio = $("globalAudio");
  const sp = $("sitePlayer");
  if (!audio) return;

  audio.addEventListener("play", function() {
    sp.classList.add("playing");
    $("spPlayIcon").querySelector("use").setAttribute("href", "#i-pause");
  });
  audio.addEventListener("pause", function() {
    sp.classList.remove("playing");
    $("spPlayIcon").querySelector("use").setAttribute("href", "#i-play");
  });
  audio.addEventListener("ended", playNextTrack);
  audio.addEventListener("timeupdate", function() {
    const slider = $("spScrubSlider");
    if (audio.duration && !isNaN(audio.duration)) {
      slider.value = (audio.currentTime / audio.duration) * 100;
      $("spCurTime").textContent = formatDuration(audio.currentTime);
      $("spDurTime").textContent = formatDuration(audio.duration);
    }
  });

  $("spScrubSlider").addEventListener("input", function() {
    if (audio.duration && !isNaN(audio.duration)) {
      audio.currentTime = (parseFloat($("spScrubSlider").value) / 100) * audio.duration;
    }
  });

  $("spPlayPause").onclick = function() {
    if (audio.paused) audio.play(); else audio.pause();
  };
  $("spNextBtn").onclick = playNextTrack;
  $("spPrevBtn").onclick = playPrevTrack;
  $("spShuffleBtn").onclick = function() {
    state.player.shuffle = !state.player.shuffle;
    updatePlayerButtons();
    toast("Shuffle: " + (state.player.shuffle ? "On" : "Off"));
  };
  $("spLoopBtn").onclick = function() {
    const order = ["all", "one", "off"];
    const next = order[(order.indexOf(state.player.loop) + 1) % order.length];
    state.player.loop = next;
    updatePlayerButtons();
    toast("Loop: " + next.toUpperCase());
  };
  $("spSpeedBtn").onclick = function() {
    const speeds = [1, 1.25, 1.5, 2, 0.75];
    const next = speeds[(speeds.indexOf(state.player.speed) + 1) % speeds.length];
    state.player.speed = next;
    audio.playbackRate = next;
    updatePlayerButtons();
  };
  $("spSleepBtn").onclick = function() {
    if (state.player.sleepTimer) {
      clearTimeout(state.player.sleepTimer);
      state.player.sleepTimer = null;
      $("spSleepBtn").classList.remove("active");
      $("spSleepBtn").textContent = "Sleep";
      toast("Sleep timer cancelled");
      return;
    }
    state.player.sleepTimer = setTimeout(function() {
      audio.pause();
      state.player.sleepTimer = null;
      $("spSleepBtn").classList.remove("active");
      $("spSleepBtn").textContent = "Sleep";
      toast("Sleep timer paused playback");
    }, 20 * 60 * 1000);
    $("spSleepBtn").classList.add("active");
    $("spSleepBtn").textContent = "20m";
    toast("Sleep timer set for 20 minutes", "success");
  };
  $("spClose").onclick = function() {
    audio.pause();
    sp.classList.remove("show", "playing");
    state.player.current = null;
    renderLibrary();
  };
  $("closeTheaterBtn").onclick = function() {
    $("theaterVideo").pause();
    $("theaterVideo").src = "";
    $("videoTheaterModal").hidden = true;
  };
  updatePlayerButtons();
}

/* ---------- Preview ---------- */
async function openPreviewByUrl(url) {
  if (!url) return;
  const modal = $("previewModal");
  modal.hidden = false;
  $("previewTitle").textContent = "Loading…";
  $("previewPlayer").innerHTML = '<div class="preview-loading">Loading…</div>';
  $("previewTags").innerHTML = "";
  try {
    const r = await fetchJSON("/api/resolve?url=" + encodeURIComponent(url));
    state.currentPreview = { url: url, data: r };
    renderPreview(r, url);
  } catch (e) {
    $("previewPlayer").innerHTML = '<div class="preview-error">Failed: ' + escapeHtml(e.message) + "</div>";
    $("previewTitle").textContent = "Error";
  }
}

function renderPreview(r, url) {
  const v = r.video || {};
  $("previewTitle").textContent = v.title || r.title || "Preview";

  if (r.preview && r.preview.type === "iframe") {
    $("previewPlayer").innerHTML = '<iframe src="' + escapeAttr(r.preview.url) + '" allow="autoplay; encrypted-media; fullscreen" allowfullscreen></iframe>';
  } else if (r.preview && r.preview.type === "video") {
    $("previewPlayer").innerHTML = '<video src="' + escapeAttr(r.preview.url) + '" controls autoplay></video>';
  } else {
    $("previewPlayer").innerHTML = '<div class="preview-none">No preview available</div>';
  }

  const tags = [];
  if (v.channel) tags.push(v.channel);
  if (v.duration) tags.push(formatDuration(v.duration));
  if (v.view_count) tags.push(formatViews(v.view_count));
  if (r.upload_date) tags.push(formatDate(r.upload_date));
  if (r.type === "playlist") tags.push(r.count + " tracks");
  $("previewTags").innerHTML = tags.map(function(t) {
    return '<span class="tag">' + escapeHtml(t) + "</span>";
  }).join("");

  const isPlaylist = r.type === "playlist";
  $("previewPlaylistRow").hidden = !isPlaylist;
  if (isPlaylist) {
    $("previewTitle").textContent = r.title + " · " + r.count + " tracks";
  }

  const fmtSel = $("previewFormat");
  fmtSel.innerHTML = '<option value="">Auto (use quality)</option>';
  const formats = (r.formats || []).filter(function(f) { return f.format_id; });
  formats.forEach(function(f) {
    const opt = document.createElement("option");
    opt.value = f.format_id;
    const parts = [f.format_id];
    if (f.ext) parts.push(f.ext);
    if (f.height) parts.push(f.height + "p");
    if (f.vcodec && f.vcodec !== "none") parts.push("v:" + String(f.vcodec).split(".")[0]);
    if (f.acodec && f.acodec !== "none") parts.push("a:" + String(f.acodec).split(".")[0]);
    if (f.filesize) parts.push(formatBytes(f.filesize));
    opt.textContent = parts.join(" · ");
    opt.dataset.acodec = f.acodec || "";
    fmtSel.appendChild(opt);
  });

  if (r.description) {
    $("previewDescWrap").hidden = false;
    $("previewDesc").textContent = r.description;
  } else {
    $("previewDescWrap").hidden = true;
  }

  const settings = state.settings || {};
  setPreviewMode(settings.mode || "mp3");
  $("previewBitrate").value = settings.abitrate || "192";
  $("previewQuality").value = settings.height || "720";
  $("previewPlaylist").checked = false;
  $("previewPlaylistLimit").value = settings.playlist_limit || 20;
  $("previewPlaylistLimitRow").hidden = true;
}

function setPreviewMode(mode) {
  $$("#previewMode .seg-btn").forEach(function(b) {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  $("previewBitrateRow").hidden = mode !== "mp3";
  $("previewQualityRow").hidden = mode !== "mp4";
  $("previewFormatRow").hidden = mode !== "mp4";
}

$$("#previewMode .seg-btn").forEach(function(b) {
  b.onclick = function() { setPreviewMode(b.dataset.mode); };
});
$("previewPlaylist").addEventListener("change", function() {
  $("previewPlaylistLimitRow").hidden = !$("previewPlaylist").checked;
});

$("previewDownloadBtn").onclick = async function() {
  const p = state.currentPreview;
  if (!p) return;
  const mode = getSegMode("#previewMode");
  const extra = {
    abitrate: $("previewBitrate").value,
    height: $("previewQuality").value,
    title: (p.data.video && p.data.video.title) || p.data.title,
    thumb: (p.data.video && p.data.video.thumbnail) || null,
    channel: (p.data.video && p.data.video.channel) || null,
    duration: (p.data.video && p.data.video.duration) || null,
    playlist: $("previewPlaylist").checked,
    playlist_limit: parseInt($("previewPlaylistLimit").value, 10) || 20
  };
  const fid = $("previewFormat").value;
  if (fid) {
    extra.format_id = fid;
    const opt = $("previewFormat").selectedOptions[0];
    extra.format_acodec = opt ? opt.dataset.acodec : "";
  }
  await queueDownload(p.url, mode, extra);
  $("previewModal").hidden = true;
};

/* ---------- Settings modal ---------- */
async function openSettings() {
  const s = state.settings || {};
  $("setTheme").value = s.theme || "dark";
  $("setThemeAuto").checked = !!s.theme_auto;
  $("setDensity").value = s.density || "comfortable";
  $("setAccent").value = s.accent || "#4d6bfe";
  $("setMode").value = s.mode || "mp3";
  $("setQuality").value = s.height || "720";
  $("setBitrate").value = s.abitrate || "192";
  $("setVideoCodec").value = s.video_codec || "any";
  $("setAutoSave").checked = s.auto_save !== false;
  $("setNotifyDone").checked = s.notify_done !== false;
  $("setAutoRetry").checked = s.auto_retry !== false;
  $("setPlaylistLimit").value = s.playlist_limit != null ? s.playlist_limit : 20;
  $("setFilenameTemplate").value = s.filename_template || "%(title).180B [%(id)s].%(ext)s";
  $("setCookies").value = s.cookies || "";
  $("setProxy").value = s.proxy || "";
  $("setRateLimit").value = s.rate_limit || "";
  $("setRetries").value = s.retries != null ? s.retries : 2;
  $("setConcurrency").value = s.concurrency != null ? s.concurrency : 4;
  $("setAutoclean").value = s.autoclean_days != null ? s.autoclean_days : 7;
  $("setOutdir").value = s.outdir || "";
  $("setEmbedThumb").checked = s.embed_thumb !== false;
  $("setEmbedMeta").checked = s.embed_meta !== false;
  $("setSubs").checked = !!s.subs;
  $("setSponsorblock").checked = !!s.sponsorblock;
  $("setNormalize").checked = !!s.normalize;
  $("setGeminiKey").value = s.gemini_key || "";
  $("settingsModal").hidden = false;
}

$("saveSettingsBtn").onclick = async function() {
  const body = {
    theme: $("setTheme").value,
    theme_auto: $("setThemeAuto").checked,
    density: $("setDensity").value,
    accent: $("setAccent").value,
    mode: $("setMode").value,
    height: $("setQuality").value,
    abitrate: $("setBitrate").value,
    video_codec: $("setVideoCodec").value,
    auto_save: $("setAutoSave").checked,
    notify_done: $("setNotifyDone").checked,
    auto_retry: $("setAutoRetry").checked,
    playlist_limit: parseInt($("setPlaylistLimit").value, 10) || 20,
    filename_template: $("setFilenameTemplate").value.trim() || "%(title).180B [%(id)s].%(ext)s",
    cookies: $("setCookies").value,
    proxy: $("setProxy").value,
    rate_limit: $("setRateLimit").value,
    retries: parseInt($("setRetries").value, 10) || 0,
    concurrency: parseInt($("setConcurrency").value, 10) || 1,
    autoclean_days: parseInt($("setAutoclean").value, 10) || 0,
    outdir: $("setOutdir").value,
    embed_thumb: $("setEmbedThumb").checked,
    embed_meta: $("setEmbedMeta").checked,
    subs: $("setSubs").checked,
    sponsorblock: $("setSponsorblock").checked,
    normalize: $("setNormalize").checked,
    gemini_key: $("setGeminiKey").value
  };
  try {
    const r = await fetchJSON("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    state.settings = r.settings || body;
    applyTheme(body.theme);
    applyAccent(body.accent);
    applyDensity(body.density);
    applyThemeAuto(body.theme_auto);
    setMode(body.mode);
    $("qualitySelect").value = body.height;
    $("bitrateSelect").value = body.abitrate;
    if (body.notify_done) requestNotifPermission();
    $("settingsModal").hidden = true;
    toast("Settings saved", "success");
    refreshJobs();
    refreshLibrary();
  } catch (e) {
    toast("Save failed: " + e.message, "error");
  }
};

$("cleanupBtn").onclick = async function() {
  try {
    const r = await fetchJSON("/api/cleanup", { method: "POST" });
    toast("Cleaned " + (r.removed || 0) + " folders", "success");
  } catch (e) {
    toast("Failed: " + e.message, "error");
  }
};

$("exportSettingsBtn").onclick = function() {
  window.location.href = "/api/settings/export";
};
$("importSettingsBtn").onclick = function() {
  $("importFileInput").click();
};
$("importFileInput").addEventListener("change", async function(ev) {
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  try {
    const text = await f.text();
    const data = JSON.parse(text);
    const r = await fetchJSON("/api/settings/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
    state.settings = r.settings || data;
    applyTheme(state.settings.theme || "dark");
    applyAccent(state.settings.accent);
    applyDensity(state.settings.density);
    applyThemeAuto(state.settings.theme_auto);
    setMode(state.settings.mode);
    $("qualitySelect").value = state.settings.height;
    $("bitrateSelect").value = state.settings.abitrate;
    toast("Settings imported", "success");
    openSettings();
  } catch (e) {
    toast("Import failed: " + e.message, "error");
  }
  ev.target.value = "";
});

$("testAiBtn").onclick = async function() {
  const key = $("setGeminiKey").value.trim() || undefined;
  try {
    const r = await fetchJSON("/api/musick/ping", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: key })
    });
    if (r.ok) toast("Connected — provider: " + r.provider, "success");
    else toast("Connection failed", "error");
  } catch (e) {
    toast("Test failed: " + e.message, "error");
  }
};

/* ---------- History modal ---------- */
async function openHistory() {
  $("historyModal").hidden = false;
  try {
    state.history = await fetchJSON("/api/history");
    renderHistory(state.history);
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderHistory(h) {
  const sWrap = $("historySearches");
  const dWrap = $("historyDownloads");
  if ((h.searches || []).length) {
    sWrap.innerHTML = h.searches.map(function(t) {
      return '<button class="chip" data-hist-term="' + escapeAttr(t) + '">' + escapeHtml(t) + "</button>";
    }).join("");
    sWrap.querySelectorAll("[data-hist-term]").forEach(function(b) {
      b.onclick = function() {
        $("historyModal").hidden = true;
        switchTab("discover");
        doSearch(b.dataset.histTerm);
      };
    });
  } else {
    sWrap.innerHTML = '<div class="muted">No searches yet.</div>';
  }
  if ((h.downloads || []).length) {
    dWrap.innerHTML = h.downloads.map(function(d) {
      return '<div class="history-item">' +
        '<div class="history-title">' + escapeHtml(d.title || d.url || "") + "</div>" +
        '<div class="history-meta">' + escapeHtml((d.mode || "").toUpperCase()) + " · " + escapeHtml(formatRelativeTime(d.time)) + "</div>" +
      "</div>";
    }).join("");
  } else {
    dWrap.innerHTML = '<div class="muted">No downloads yet.</div>';
  }
}

$("clearSearchesBtn").onclick = async function() {
  try {
    await fetchJSON("/api/history?kind=searches", { method: "DELETE" });
    state.history.searches = [];
    renderChips();
    updateSuggestions();
    renderHistory(state.history);
  } catch (e) { toast(e.message, "error"); }
};
$("clearDownloadsBtn").onclick = async function() {
  try {
    await fetchJSON("/api/history?kind=downloads", { method: "DELETE" });
    state.history.downloads = [];
    renderHistory(state.history);
  } catch (e) { toast(e.message, "error"); }
};

/* ---------- Batch ---------- */
$("batchBtn").onclick = function() {
  $("batchModal").hidden = false;
  $("batchTextarea").value = "";
  setTimeout(function() { $("batchTextarea").focus(); }, 50);
};
$("batchQueueBtn").onclick = async function() {
  const urls = $("batchTextarea").value.split("\n").map(function(s) { return s.trim(); }).filter(Boolean);
  if (!urls.length) return;
  const mode = getSegMode("#modeToggle");
  let count = 0;
  for (const u of urls) {
    try { await queueDownload(u, mode); count++; } catch (e) {}
  }
  $("batchModal").hidden = true;
  toast("Queued " + count + " / " + urls.length, "success");
};

/* ---------- Top bar wiring ---------- */
if ($("globalDlPill")) {
  $("globalDlPill").onclick = function() {
    switchTab("library");
  };
}
$("searchBtn").onclick = function() { doSearch($("searchInput").value); };
$("searchInput").addEventListener("keydown", function(e) {
  if (e.key === "Enter") { e.preventDefault(); doSearch($("searchInput").value); }
});
$("pasteBtn").onclick = async function() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      $("searchInput").value = text.trim();
      $("searchInput").focus();
      toast("Pasted");
    }
  } catch (e) {
    toast("Clipboard unavailable", "error");
  }
};

$$("#modeToggle .seg-btn").forEach(function(b) {
  b.onclick = function() {
    setMode(b.dataset.mode);
    if (state.settings) {
      state.settings.mode = b.dataset.mode;
      fetchJSON("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({}, state.settings, { mode: b.dataset.mode }))
      }).catch(function() {});
    }
    if (state.results.length) renderResults();
  };
});
$("qualitySelect").onchange = function() {
  if (state.settings) {
    state.settings.height = $("qualitySelect").value;
    fetchJSON("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({}, state.settings, { height: $("qualitySelect").value }))
    }).catch(function() {});
  }
  if (state.results.length) renderResults();
};
$("bitrateSelect").onchange = function() {
  if (state.settings) {
    state.settings.abitrate = $("bitrateSelect").value;
    fetchJSON("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({}, state.settings, { abitrate: $("bitrateSelect").value }))
    }).catch(function() {});
  }
};
$("lengthFilter").onchange = function() { if (state.results.length) renderResults(); };
$("sortSelect").onchange = function() { if (state.results.length) renderResults(); };
$("themeBtn").onclick = function() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next);
  if (state.settings) {
    state.settings.theme = next;
    fetchJSON("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({}, state.settings, { theme: next }))
    }).catch(function() {});
  }
};
$("historyBtn").onclick = openHistory;
$("settingsBtn").onclick = openSettings;
$("shortcutsBtn").onclick = function() { $("shortcutsModal").hidden = false; };

/* ---------- Drag-drop URL ---------- */
document.addEventListener("dragover", function(e) {
  if (e.dataTransfer && e.dataTransfer.types.indexOf("text/uri-list") >= 0) {
    e.preventDefault();
  }
});
document.addEventListener("drop", function(e) {
  const dt = e.dataTransfer;
  if (!dt) return;
  if (dt.files && dt.files.length) {
    const f = dt.files[0];
    if (f && (f.type.startsWith("audio/") || f.type.startsWith("video/"))) {
      e.preventDefault();
      playLocalFile(f);
      toast("Playing local file: " + f.name, "success");
      return;
    }
  }
  const text = dt.getData("text/uri-list") || dt.getData("text/plain");
  if (text && /^https?:\/\//i.test(text.trim())) {
    e.preventDefault();
    switchTab("discover");
    $("searchInput").value = text.trim();
    doSearch(text.trim());
  }
});

/* ---------- Modal close buttons / Esc ---------- */
$$("[data-close]").forEach(function(b) {
  b.onclick = function() {
    const m = $(b.dataset.close);
    if (m) m.hidden = true;
  };
});
$$(".modal-backdrop").forEach(function(bd) {
  bd.addEventListener("click", function(e) {
    if (e.target === bd) bd.hidden = true;
  });
});

/* ---------- Keyboard shortcuts & Media Controls ---------- */
document.addEventListener("keydown", function(e) {
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  if (e.key === "Escape") {
    let closed = false;
    $$(".modal-backdrop").forEach(function(m) {
      if (!m.hidden) {
        if (m.id === "videoTheaterModal") {
          $("theaterVideo").pause();
          $("theaterVideo").src = "";
        }
        m.hidden = true;
        closed = true;
      }
    });
    if (state.chat.open) { closeChat(); closed = true; }
    if (closed) e.preventDefault();
  } else if (e.key === "/" && !inInput) {
    e.preventDefault();
    switchTab("discover");
    $("searchInput").focus();
  } else if (e.key === "?" && !inInput) {
    e.preventDefault();
    $("shortcutsModal").hidden = false;
  } else if (!inInput && state.player.current && state.player.current.ext !== "mp4") {
    const audio = $("globalAudio");
    if (e.key === " ") {
      e.preventDefault();
      if (audio.paused) audio.play(); else audio.pause();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      audio.currentTime = Math.max(0, audio.currentTime - 5);
    }
  }
});

/* ---------- Persistent Chat Panel (Never clears on close!) ---------- */
$("fabBtn").onclick = openChat;
$("closeChatBtn").onclick = closeChat;
$("newChatBtn").onclick = newChat;
$("chatExportBtn").onclick = exportChat;

function openChat() {
  $("chatPanel").hidden = false;
  $("fabBtn").hidden = true;
  state.chat.open = true;
  if (!state.chat.initialized) {
    loadChatFromStorage();
    renderChatMessages();
    state.chat.initialized = true;
  } else {
    updateAllCardProgress();
  }
  setTimeout(function() { $("chatInput").focus(); scrollChatToBottom(); }, 60);
}
function closeChat() {
  // Simply hide the panel; keep DOM & state intact so nothing ever disappears
  $("chatPanel").hidden = true;
  $("fabBtn").hidden = false;
  state.chat.open = false;
}
function newChat() {
  state.chat.messages = [];
  saveChatToStorage();
  $("chatMessages").innerHTML = "";
  $("chatEmpty").hidden = false;
  wireChatEmpty();
}
function exportChat() {
  if (!state.chat.messages.length) { toast("Chat is empty", "error"); return; }
  const lines = [];
  state.chat.messages.forEach(function(m) {
    const who = m.role === "user" ? "You" : "Musick AI";
    let body = m.content || "";
    if (m.thinking) body = "(thinking)\n" + m.thinking + "\n\n" + body;
    lines.push(who + ":\n" + body);
  });
  const text = lines.join("\n\n---\n\n");
  const blob = new Blob([text], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "musick-chat-" + Date.now() + ".txt";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast("Chat exported", "success");
}
function loadChatFromStorage() {
  try {
    const raw = localStorage.getItem("musick.chat.v2");
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        state.chat.messages = arr.filter(function(m) {
          return m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string";
        }).map(function(m) {
          return {
            role: m.role,
            content: m.content || "",
            thinking: m.thinking || "",
            actions: m.actions || [],
            inlineResults: Array.isArray(m.inlineResults) ? m.inlineResults : []
          };
        });
      }
    }
  } catch (e) {}
}
function saveChatToStorage() {
  try {
    const arr = state.chat.messages.slice(-40).map(function(m) {
      return {
        role: m.role,
        content: m.content || "",
        thinking: m.thinking || "",
        actions: m.actions || [],
        inlineResults: Array.isArray(m.inlineResults) ? m.inlineResults : []
      };
    });
    localStorage.setItem("musick.chat.v2", JSON.stringify(arr));
  } catch (e) {}
}
function wireChatEmpty() {
  $$(".suggestion-chip").forEach(function(b) {
    b.onclick = function() { if (b.dataset.suggestion) sendChat(b.dataset.suggestion); };
  });
}

function renderChatMessages() {
  const has = state.chat.messages.length > 0;
  $("chatEmpty").hidden = has;
  const c = $("chatMessages");
  c.innerHTML = "";
  state.chat.messages.forEach(function(m) {
    c.appendChild(renderChatMessage(m));
  });
  updateAllCardProgress();
  scrollChatToBottom();
}

function renderChatMessage(m) {
  const row = document.createElement("div");
  row.className = "msg-row " + (m.role === "user" ? "user" : "bot");
  const avatar = m.role === "user"
    ? '<svg class="icon"><use href="#i-music"/></svg>'
    : '<svg class="icon"><use href="#i-brain"/></svg>';
  row.innerHTML = '<div class="msg-avatar">' + avatar + '</div><div class="bubble"></div>';
  updateChatBubble(m, row);
  return row;
}

function updateChatBubble(m, row) {
  const bubble = row.querySelector(".bubble");
  let html = "";

  if (m.role === "assistant" && m.thinking) {
    const isStreaming = m.streaming && !m.thinkingEnd;
    const seconds = m.thinkingStart
      ? Math.max(1, Math.round(((m.thinkingEnd || Date.now()) - m.thinkingStart) / 1000))
      : 0;
    const summary = isStreaming ? "Thinking…" : "Thought for " + seconds + "s";
    html += '<details class="thinking"' + (isStreaming ? " open" : "") + ">" +
      '<summary><svg class="icon chev"><use href="#i-chevron"/></svg><span>' + summary + "</span></summary>" +
      '<div class="thinking-body">' + escapeHtml(m.thinking) + (isStreaming ? '<span class="caret"></span>' : "") + "</div>" +
    "</details>";
  }

  if (m.content) {
    const display = m.streaming ? stripPartialAction(m.content) : m.content;
    html += '<div class="md">' + md(display) + (m.streaming ? '<span class="caret"></span>' : "") + "</div>";
  } else if (m.streaming && !m.thinking) {
    html += '<div class="md" style="color:var(--text-dim)">…</div>';
  }

  if (m.error) {
    html += '<div class="chat-error">' + escapeHtml(m.error) + "</div>";
  }

  if (m.inlineResults && m.inlineResults.length) {
    html += '<div class="inline-results">' + m.inlineResults.map(renderInlineCard).join("") + "</div>";
  }

  if (!m.streaming && m.content) {
    html += '<div class="msg-actions">' +
      '<button data-act="copy">Copy</button>' +
      (m.role === "assistant" ? '<button data-act="regen">Regenerate</button>' : '<button data-act="edit">Edit</button>') +
      '<button data-act="delete" style="color:var(--danger)">Delete</button>' +
    "</div>";
  }

  bubble.innerHTML = html;

  bubble.querySelectorAll("pre.code-block").forEach(function(pre) {
    if (pre.querySelector(".code-copy-btn")) return;
    const btn = document.createElement("button");
    btn.className = "code-copy-btn";
    btn.textContent = "Copy";
    btn.onclick = function(e) {
      e.stopPropagation();
      const code = pre.querySelector("code");
      const text = code ? code.textContent : pre.textContent;
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = "Copied";
        setTimeout(() => { btn.textContent = "Copy"; }, 1200);
      });
    };
    pre.appendChild(btn);
  });

  bubble.querySelectorAll('[data-act="copy"]').forEach(function(b) {
    b.onclick = function() { copyText(m.content); };
  });
  bubble.querySelectorAll('[data-act="regen"]').forEach(function(b) {
    b.onclick = function() { regenerate(m); };
  });
  bubble.querySelectorAll('[data-act="edit"]').forEach(function(b) {
    b.onclick = function() { startEdit(m, row); };
  });
  bubble.querySelectorAll('[data-act="delete"]').forEach(function(b) {
    b.onclick = function() {
      if (b.dataset.armed === "1") {
        deleteMessage(m, row);
      } else {
        b.dataset.armed = "1";
        b.textContent = "Confirm?";
        setTimeout(function() {
          if (b && b.parentNode) {
            b.dataset.armed = "0";
            b.textContent = "Delete";
          }
        }, 2500);
      }
    };
  });
  bubble.querySelectorAll("[data-inline-preview]").forEach(function(b) {
    b.onclick = function() { openPreviewByUrl(b.dataset.inlinePreview); };
  });
  bubble.querySelectorAll("[data-inline-mp3]").forEach(function(b) {
    b.onclick = function() {
      const item = (m.inlineResults || []).find(x => x.url === b.dataset.inlineMp3) || {};
      queueDownload(b.dataset.inlineMp3, "mp3", { title: item.title, thumb: item.thumbnail, channel: item.channel, duration: item.duration });
    };
  });
  bubble.querySelectorAll("[data-inline-mp4]").forEach(function(b) {
    b.onclick = function() {
      const item = (m.inlineResults || []).find(x => x.url === b.dataset.inlineMp4) || {};
      queueDownload(b.dataset.inlineMp4, "mp4", { title: item.title, thumb: item.thumbnail, channel: item.channel, duration: item.duration });
    };
  });
  bubble.querySelectorAll("[data-inline-play]").forEach(function(b) {
    b.onclick = function() {
      const job = state.jobs.find(j => j.id === b.dataset.inlinePlay);
      if (job) playJobMedia(job);
    };
  });

  updateAllCardProgress();
}

function startEdit(m, row) {
  const bubble = row.querySelector(".bubble");
  const original = m.content;
  bubble.innerHTML = '<div class="edit-wrap"><textarea>' + escapeHtml(original) + '</textarea></div>' +
    '<div class="edit-btns"><button class="primary" data-edit-save>Save &amp; resend</button><button data-edit-cancel>Cancel</button></div>';
  const ta = bubble.querySelector("textarea");
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  bubble.querySelector("[data-edit-save]").onclick = async function() {
    const newText = ta.value.trim();
    if (!newText) return;
    const idx = state.chat.messages.indexOf(m);
    if (idx < 0) return;
    state.chat.messages = state.chat.messages.slice(0, idx);
    saveChatToStorage();
    renderChatMessages();
    await sendChat(newText);
  };
  bubble.querySelector("[data-edit-cancel]").onclick = function() {
    updateChatBubble(m, row);
  };
}

function renderInlineCard(r) {
  const thumbUrl = r.thumbnail ? ("/api/thumb?url=" + encodeURIComponent(r.thumbnail)) : "";
  return '<div class="inline-card" data-inline-url="' + escapeAttr(r.url) + '">' +
    '<div class="inline-card-top">' +
      '<img src="' + thumbUrl + '" alt="" loading="lazy" referrerpolicy="no-referrer">' +
      '<div class="inline-info">' +
        '<div class="inline-title">' + escapeHtml(r.title || "") + "</div>" +
        '<div class="inline-channel">' + escapeHtml(r.channel || "") + (r.duration ? " · " + formatDuration(r.duration) : "") + "</div>" +
      "</div>" +
      '<div class="inline-actions">' +
        '<button class="mini-btn" data-inline-play="" hidden title="Play"><svg class="icon"><use href="#i-play"/></svg></button>' +
        '<button class="mini-btn" data-inline-preview="' + escapeAttr(r.url) + '" title="Preview"><svg class="icon"><use href="#i-eye"/></svg></button>' +
        '<button class="mini-btn" data-inline-mp3="' + escapeAttr(r.url) + '" title="MP3"><svg class="icon"><use href="#i-music"/></svg></button>' +
        '<button class="mini-btn" data-inline-mp4="' + escapeAttr(r.url) + '" title="MP4"><svg class="icon"><use href="#i-video"/></svg></button>' +
      "</div>" +
    "</div>" +
    '<div class="inline-progress-wrap">' +
      '<div class="inline-progress-meta"><span class="ip-stage">Starting</span><span class="ip-pct">0%</span></div>' +
      '<div class="inline-progress-bar"><div class="inline-progress-fill"></div></div>' +
    "</div>" +
  "</div>";
}

function stripPartialAction(text) {
  const idx = text.search(/```json|\{"action"/);
  if (idx >= 0) return text.slice(0, idx).trim();
  return text;
}

function extractActions(text) {
  if (!text) return { text: text || "", actions: [] };
  const actions = [];
  let clean = text;
  const fenced = /```json\s*(\{[\s\S]*?\})\s*```/g;
  let m;
  while ((m = fenced.exec(text)) !== null) {
    try {
      const a = JSON.parse(m[1]);
      if (a && a.action) actions.push(a);
      clean = clean.replace(m[0], "");
    } catch (e) {}
  }
  if (!actions.length) {
    const raw = /(\{"action"[\s\S]*?\})/g;
    while ((m = raw.exec(clean)) !== null) {
      try {
        const a = JSON.parse(m[1]);
        if (a && a.action) actions.push(a);
        clean = clean.replace(m[1], "");
      } catch (e) {}
    }
  }
  return { text: clean.trim(), actions: actions };
}

function scrollChatToBottom() {
  const body = $("chatBody");
  body.scrollTop = body.scrollHeight;
  const btn = $("scrollBottomBtn");
  if (btn) btn.classList.remove("show");
}

function showTypingIndicator(show) {
  const el = $("typingIndicator");
  if (!el) return;
  if (show) { el.classList.add("show"); maybeScrollChatToBottom(); }
  else { el.classList.remove("show"); }
}

function updateScrollBottomBtn() {
  const body = $("chatBody");
  const btn = $("scrollBottomBtn");
  if (!body || !btn) return;
  const near = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
  if (near) btn.classList.remove("show");
  else if (state.chat.open) btn.classList.add("show");
}

function confettiBurst() {
  const colors = ["#4d6bfe", "#7c3aed", "#4caf78", "#e0b04c", "#e05252", "#f5b93d"];
  for (let i = 0; i < 40; i++) {
    const p = document.createElement("div");
    p.className = "confetti-piece";
    p.style.left = (Math.random() * 100) + "vw";
    p.style.top = "-10vh";
    p.style.background = colors[Math.floor(Math.random() * colors.length)];
    p.style.animationDelay = (Math.random() * 0.4) + "s";
    p.style.transform = "rotate(" + (Math.random() * 360) + "deg)";
    document.body.appendChild(p);
    setTimeout(function() { p.remove(); }, 3000);
  }
}

let soundCtx = null;
function playChime() {
  try {
    if (!soundCtx) soundCtx = new (window.AudioContext || window.webkitAudioContext)();
    const now = soundCtx.currentTime;
    [660, 880, 1320].forEach(function(freq, i) {
      const o = soundCtx.createOscillator();
      const g = soundCtx.createGain();
      o.frequency.value = freq;
      o.type = "sine";
      o.connect(g); g.connect(soundCtx.destination);
      g.gain.setValueAtTime(0, now + i * 0.12);
      g.gain.linearRampToValueAtTime(0.06, now + i * 0.12 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.12 + 0.35);
      o.start(now + i * 0.12);
      o.stop(now + i * 0.12 + 0.4);
    });
  } catch (e) {}
}

function haptic() {
  if (navigator.vibrate) {
    try { navigator.vibrate([30, 40, 30]); } catch (e) {}
  }
}

function loadFavorites() {
  try { return JSON.parse(localStorage.getItem("musick.favorites.v1") || "[]"); }
  catch (e) { return []; }
}
function saveFavorites(list) {
  try { localStorage.setItem("musick.favorites.v1", JSON.stringify(list)); } catch (e) {}
}
function isFavorite(url) {
  return loadFavorites().some(function(f) { return f.url === url; });
}
function toggleFavorite(url, title, thumb) {
  let list = loadFavorites();
  const idx = list.findIndex(function(f) { return f.url === url; });
  if (idx >= 0) { list.splice(idx, 1); }
  else { list.unshift({ url: url, title: title, thumb: thumb, ts: Date.now() }); }
  list = list.slice(0, 200);
  saveFavorites(list);
  return idx < 0;
}

state.jobFilter = "all";
function applyJobFilter() {
  const filter = state.jobFilter;
  const cards = $("jobsList").querySelectorAll(".job-card");
  cards.forEach(function(c) {
    const status = (c.querySelector(".job-status") || {}).textContent || "";
    let show = false;
    if (filter === "all") show = true;
    else if (filter === "active") show = ["queued", "starting", "downloading", "processing"].indexOf(status) >= 0;
    else if (filter === "done") show = status === "done";
    else if (filter === "error") show = status === "error" || status === "cancelled";
    c.style.display = show ? "" : "none";
  });
  const counts = { all: state.jobs.length, active: 0, done: 0, error: 0 };
  state.jobs.forEach(function(j) {
    if (["queued", "starting", "downloading", "processing"].indexOf(j.status) >= 0) counts.active++;
    else if (j.status === "done") counts.done++;
    else if (j.status === "error" || j.status === "cancelled") counts.error++;
  });
  $$(".filter-tab").forEach(function(t) {
    t.classList.toggle("active", t.dataset.filter === filter);
    const c = t.querySelector(".fc");
    if (c) c.textContent = counts[t.dataset.filter] || "";
  });
}

state.chatSearch = "";
function applyChatSearch() {
  const q = state.chatSearch.toLowerCase().trim();
  const rows = $("chatMessages").querySelectorAll(".msg-row");
  rows.forEach(function(r) {
    if (!q) { r.style.display = ""; return; }
    const text = (r.textContent || "").toLowerCase();
    r.style.display = text.indexOf(q) >= 0 ? "" : "none";
  });
}

function deleteMessage(msg, row) {
  const idx = state.chat.messages.indexOf(msg);
  if (idx < 0) return;
  state.chat.messages.splice(idx, 1);
  row.remove();
  saveChatToStorage();
  if (!state.chat.messages.length) $("chatEmpty").hidden = false;
}
function maybeScrollChatToBottom() {
  const body = $("chatBody");
  const near = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
  if (near) body.scrollTop = body.scrollHeight;
}

async function sendChat(text) {
  text = (text || "").trim();
  if (!text || state.chat.streaming) return;
  const c = $("chatMessages");

  const userMsg = { role: "user", content: text };
  state.chat.messages.push(userMsg);
  c.appendChild(renderChatMessage(userMsg));
  $("chatEmpty").hidden = true;
  $("chatInput").value = "";
  autosizeChatInput();
  saveChatToStorage();

  const botMsg = {
    role: "assistant",
    content: "",
    thinking: "",
    streaming: true,
    thinkingStart: null,
    thinkingEnd: null,
    actions: [],
    inlineResults: [],
    error: null
  };
  state.chat.messages.push(botMsg);
  const botEl = renderChatMessage(botMsg);
  c.appendChild(botEl);
  state.chat.streaming = true;
  state.chat.streamStart = Date.now();
  showTypingIndicator(true);
  updateSendButton();
  scrollChatToBottom();

  const payload = state.chat.messages
    .filter(function(m) { return !m.streaming; })
    .map(function(m) { return { role: m.role, content: m.content }; });

  try {
    await streamChat(payload, botMsg, botEl);
  } catch (e) {
    if (e.name !== "AbortError") botMsg.error = String(e.message || e);
  }
  botMsg.streaming = false;
  state.chat.streaming = false;
  state.chat.abort = null;
  showTypingIndicator(false);
  updateSendButton();
  updateChatBubble(botMsg, botEl);
  maybeScrollChatToBottom();
  saveChatToStorage();

  if (botMsg.actions && botMsg.actions.length) {
    for (const act of botMsg.actions) {
      await executeChatAction(act, botMsg, botEl);
    }
    saveChatToStorage();
  }
}

async function streamChat(messages, botMsg, botEl) {
  const ctrl = new AbortController();
  state.chat.abort = ctrl;
  const res = await fetch("/api/musick/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: messages }),
    signal: ctrl.signal
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  if (!res.body) throw new Error("no stream body");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  let pending = false;
  let lastRender = 0;
  function scheduleRender(force) {
    const now = Date.now();
    if (!force && now - lastRender < 70) {
      if (pending) return;
      pending = true;
      setTimeout(function() {
        pending = false;
        lastRender = Date.now();
        updateChatBubble(botMsg, botEl);
        maybeScrollChatToBottom();
      }, 70 - (now - lastRender));
      return;
    }
    lastRender = now;
    updateChatBubble(botMsg, botEl);
    maybeScrollChatToBottom();
  }

  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const lines = raw.split("\n");
        for (const line of lines) {
          if (!line || line.startsWith(":")) continue;
          if (!line.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
          if (ev.type && ev.type !== "_keepalive" && ev.type !== "done") showTypingIndicator(false);
          if (ev.type === "thinking_start") {
            botMsg.thinkingStart = Date.now();
            botMsg.thinkingEnd = null;
            scheduleRender(true);
          } else if (ev.type === "thinking") {
            if (!botMsg.thinkingStart) botMsg.thinkingStart = Date.now();
            botMsg.thinking += ev.content || "";
            scheduleRender();
          } else if (ev.type === "thinking_end") {
            botMsg.thinkingEnd = Date.now();
            scheduleRender(true);
          } else if (ev.type === "response") {
            botMsg.content += ev.content || "";
            scheduleRender();
          } else if (ev.type === "error") {
            botMsg.error = ev.message || "Error";
            scheduleRender(true);
          }
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      botMsg.content += "\n\n_(stopped)_";
    } else {
      throw e;
    }
  }

  const parsed = extractActions(botMsg.content);
  botMsg.content = parsed.text;
  botMsg.actions = parsed.actions;
  updateChatBubble(botMsg, botEl);
  maybeScrollChatToBottom();
}

async function executeChatAction(action, botMsg, botEl) {
  try {
    if (action.action === "search") {
      const q = action.query;
      if (!q) return;
      const n = parseInt($("countSelect").value, 10) || 8;
      const r = await fetchJSON("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: q, n: n })
      });
      const existing = botMsg.inlineResults || [];
      const newItems = (r.results || []).slice(0, 6);
      botMsg.inlineResults = existing.concat(newItems);
      updateChatBubble(botMsg, botEl);
      maybeScrollChatToBottom();
      state.results = r.results || [];
      state.resultsQuery = q;
      renderResults();
      $("resultsSection").hidden = state.results.length === 0;
      refreshHistory();
    } else if (action.action === "download") {
      if (action.url) {
        await queueDownload(action.url, action.mode || "mp3");
      } else if (action.query) {
        await queueDownloadQuery(action.query, action.mode || "mp3");
      }
    } else if (action.action === "download_query") {
      if (action.query) {
        const r = await fetchJSON("/api/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ q: action.query, n: 3 })
        });
        const top = (r.results || [])[0];
        if (top) {
          botMsg.inlineResults = (botMsg.inlineResults || []).concat([top]);
          updateChatBubble(botMsg, botEl);
          await queueDownload(top.url, action.mode || "mp3", {
            title: top.title, thumb: top.thumbnail, channel: top.channel, duration: top.duration
          });
        } else {
          await queueDownloadQuery(action.query, action.mode || "mp3");
        }
      }
    } else if (action.action === "play") {
      await openPreviewByUrl(action.url);
    }
  } catch (e) {
    botMsg.error = "Action failed: " + e.message;
    updateChatBubble(botMsg, botEl);
  }
}

async function regenerate(botMsg) {
  const idx = state.chat.messages.indexOf(botMsg);
  if (idx < 0) return;
  let userIdx = -1;
  for (let i = idx - 1; i >= 0; i--) {
    if (state.chat.messages[i].role === "user") { userIdx = i; break; }
  }
  if (userIdx < 0) return;
  const userText = state.chat.messages[userIdx].content;
  state.chat.messages = state.chat.messages.slice(0, userIdx);
  saveChatToStorage();
  renderChatMessages();
  await sendChat(userText);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || "");
    toast("Copied", "success");
  } catch (e) {
    toast("Copy failed", "error");
  }
}

function updateSendButton() {
  const btn = $("chatSendBtn");
  const use = btn.querySelector("use");
  if (state.chat.streaming) {
    btn.classList.add("stop");
    if (use) use.setAttribute("href", "#i-square");
  } else {
    btn.classList.remove("stop");
    if (use) use.setAttribute("href", "#i-send");
  }
}

function autosizeChatInput() {
  const ta = $("chatInput");
  ta.style.height = "auto";
  const h = Math.min(100, Math.max(22, ta.scrollHeight));
  ta.style.height = h + "px";
}

$("chatInput").addEventListener("input", autosizeChatInput);
$("chatInput").addEventListener("keydown", function(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (state.chat.streaming) return;
    sendChat($("chatInput").value);
  }
});
document.addEventListener("keydown", function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && state.chat.open) {
    e.preventDefault();
    if (!state.chat.streaming) sendChat($("chatInput").value);
  }
});
$("chatSendBtn").onclick = function() {
  if (state.chat.streaming) {
    if (state.chat.abort) {
      try { state.chat.abort.abort(); } catch (e) {}
    }
  } else {
    sendChat($("chatInput").value);
  }
};

if ($("chatSearchBtn")) $("chatSearchBtn").onclick = function() {
  const w = $("chatSearchWrap");
  w.classList.toggle("show");
  if (w.classList.contains("show")) $("chatSearchInput").focus();
  else { $("chatSearchInput").value = ""; state.chatSearch = ""; applyChatSearch(); }
};
if ($("chatSearchInput")) $("chatSearchInput").oninput = function(e) {
  state.chatSearch = e.target.value;
  applyChatSearch();
};
if ($("chatSearchClose")) $("chatSearchClose").onclick = function() {
  $("chatSearchWrap").classList.remove("show");
  $("chatSearchInput").value = "";
  state.chatSearch = "";
  applyChatSearch();
};
if ($("chatBody")) $("chatBody").addEventListener("scroll", updateScrollBottomBtn);
if ($("scrollBottomBtn")) $("scrollBottomBtn").onclick = scrollChatToBottom;
$$(".filter-tab").forEach(function(t) {
  t.onclick = function() {
    state.jobFilter = t.dataset.filter;
    applyJobFilter();
  };
});

wireSitePlayer();

function playLocalFile(file) {
  const url = URL.createObjectURL(file);
  const isVideo = file.type.startsWith("video/");
  playLibraryQueue([{
    id: "local_" + Date.now(),
    title: file.name.replace(/\.[^.]+$/, ""),
    channel: "Local · " + formatBytes(file.size),
    thumb: "",
    ext: isVideo ? "mp4" : "mp3",
    stream_url: url
  }], 0);
}

if ($("chatMediaBtn")) $("chatMediaBtn").onclick = function() { $("chatMediaInput").click(); };
if ($("chatMediaInput")) $("chatMediaInput").onchange = function(e) {
  const f = e.target.files && e.target.files[0];
  if (!f) return;
  playLocalFile(f);
  e.target.value = "";
};

/* ---------- Boot ---------- */
async function boot() {
  wireChatEmpty();
  loadChatFromStorage();
  await loadSettings();
  await refreshHistory();
  refreshHealth();
  refreshJobs();
  refreshLibrary();
  setInterval(refreshHealth, 30000);
  setTimeout(autosizeChatInput, 100);
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    notifPermission = "granted";
  }
  requestNotifPermission().catch(function() {});
}
boot();

})();
</script>
</body>
</html>
'''


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    if not HAS_YTDLP:
        print("! yt-dlp not importable — install with: pip install yt-dlp")
    if not HAS_FFMPEG:
        print("! ffmpeg not found on PATH — required for MP3 extraction and MP4 merging")
    if not HAS_FFPROBE:
        print("! ffprobe not found on PATH — used for format detection")

    port = int(os.environ.get("PORT", "8080"))
    local_ip = _local_ip()
    print("")
    print("  Musick AI is running")
    print("  -> http://127.0.0.1:{}".format(port))
    print("  -> http://{}:{}   (from devices on your LAN)".format(local_ip, port))
    print("  -> output dir: {}".format(load_settings().get("outdir")))
    print("  -> data dir:   {}".format(DATA_DIR))
    print("")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
