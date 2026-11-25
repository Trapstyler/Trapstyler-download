#!/usr/bin/env python3
"""
Musick backend — endpoint-only (youtubei / vimeo config / lrclib). No yt-dlp.
Run:  python app.py
Deps: pip install flask requests
"""
import os, re, json, time, threading, urllib.parse, mimetypes, subprocess
from pathlib import Path

import requests
from flask import Flask, request, Response, jsonify, send_file, stream_with_context, send_from_directory

PORT         = int(os.environ.get("PORT", 8000))
BASE_DIR     = Path(__file__).parent.resolve()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA_MOBILE = "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip"
UA_WEB    = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

CLIENTS = [
    {"name": "ANDROID", "ver": "19.09.37", "ua": UA_MOBILE,
     "ctx": {"client": {"clientName": "ANDROID", "clientVersion": "19.09.37",
                        "androidSdkVersion": 30, "hl": "en", "gl": "US"}}},
    {"name": "IOS", "ver": "19.09.3",
     "ua": "com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)",
     "ctx": {"client": {"clientName": "IOS", "clientVersion": "19.09.3",
                        "deviceModel": "iPhone14,3", "hl": "en", "gl": "US"}}},
    {"name": "WEB_REMIX", "ver": "1.20250317.01.00", "ua": UA_WEB,
     "ctx": {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20250317.01.00",
                        "platform": "DESKTOP", "hl": "en", "gl": "US"}}},
    {"name": "WEB", "ver": "2.20240726.00.00", "ua": UA_WEB,
     "ctx": {"client": {"clientName": "WEB", "clientVersion": "2.20240726.00.00",
                        "hl": "en", "gl": "US"}}},
]

app = Flask(__name__, static_folder=None)
JOBS, JOBS_LOCK = {}, threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _proxy(raw: str) -> str:
    if not raw: return ""
    return "/api/proxy?url=" + urllib.parse.quote(raw, safe="")

def _video_id(url: str):
    for pat in (r"v=([\w-]{11})", r"youtu\.be/([\w-]{11})",
                r"shorts/([\w-]{11})", r"embed/([\w-]{11})",
                r"music\.youtube\.com/watch\?v=([\w-]{11})"):
        m = re.search(pat, url or "")
        if m: return m.group(1)
    return None

def _vimeo_id(url: str):
    m = re.search(r"vimeo\.com/(?:video/)?(\d+)", url or "")
    return m.group(1) if m else None

def _parse_duration(s: str) -> int:
    if not s: return 0
    parts = s.strip().lstrip(":").split(":")
    try: nums = [int(p) for p in parts if p.isdigit()]
    except ValueError: return 0
    if len(nums) == 1: return nums[0]
    if len(nums) == 2: return nums[0]*60 + nums[1]
    if len(nums) == 3: return nums[0]*3600 + nums[1]*60 + nums[2]
    return 0

def _parse_views(s: str) -> int:
    if not s: return 0
    m = re.search(r"([\d.]+)\s*([KMB])?", s.replace(",", ""))
    if not m: return 0
    n = float(m.group(1))
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2) or "", 1)
    return int(n * mult)

# ---------------------------------------------------------------------------
# YouTube Music search  (mirrors s_web)
# ---------------------------------------------------------------------------
def _ytm_search(query: str, limit: int) -> list:
    body = {
        "query":   query,
        "params":  "Eg-KAQwIARAAGAAgACgAMABqChAEEAUQAxAKEAk%3D",
        "context": CLIENTS[2]["ctx"],
    }
    url = f"https://music.youtube.com/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false"
    headers = {"User-Agent": CLIENTS[2]["ua"], "Content-Type": "application/json",
               "X-YouTube-Client-Name": "67",
               "X-YouTube-Client-Version": CLIENTS[2]["ver"],
               "Origin": "https://music.youtube.com",
               "Referer": "https://music.youtube.com/"}
    last_err = None; r = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, headers=headers, timeout=(5, 20))
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e; time.sleep(0.5 * (attempt + 1)); continue
        except requests.HTTPError as e:
            last_err = e; break
    if r is None:
        raise RuntimeError(f"YTM unreachable: {last_err}")
    data = r.json()

    items = []
    try:
        shelf = (data["contents"]["tabbedSearchResultsRenderer"]["tabs"][0]
                 ["tabRenderer"]["content"]["sectionListRenderer"]["contents"][0]
                 ["musicShelfRenderer"]["contents"])
    except (KeyError, IndexError, TypeError):
        return _yt_plain_search(query, limit)

    for it in shelf:
        r0 = it.get("musicResponsiveListItemRenderer")
        if not r0: continue
        try:
            cols = r0["flexColumns"]
            title    = cols[0]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0]["text"]
            runs1    = cols[1]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
            artist   = runs1[0]["text"] if runs1 else ""
            duration = runs1[-1]["text"] if runs1 else ""
            views    = ""
            if len(cols) > 2:
                views = cols[2]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"][0]["text"]
            cover = r0["thumbnail"]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"][0]["url"]
            vid   = r0["playlistItemData"]["videoId"]
            items.append({
                "id": vid, "title": title, "channel": artist,
                "duration": _parse_duration(duration),
                "view_count": _parse_views(views),
                "thumbnail": cover,
                "url": f"https://www.youtube.com/watch?v={vid}",
            })
        except (KeyError, IndexError, TypeError):
            continue
        if len(items) >= limit: break
    return items

# ---------------------------------------------------------------------------
# YouTube player  (mirrors p_web adaptiveFormats)
# ---------------------------------------------------------------------------

def _yt_plain_search(query: str, limit: int) -> list:
    """Fallback: plain youtubei search with WEB client."""
    body = {"query": query, "context": CLIENTS[3]["ctx"]}
    r = requests.post(
        f"https://www.youtube.com/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false",
        json=body, timeout=(5, 20),
        headers={"User-Agent": CLIENTS[3]["ua"], "Content-Type": "application/json",
                 "X-YouTube-Client-Name": "1",
                 "X-YouTube-Client-Version": CLIENTS[3]["ver"],
                 "Origin": "https://www.youtube.com",
                 "Referer": "https://www.youtube.com/"},
    )
    r.raise_for_status()
    data = r.json()
    items = []
    try:
        contents = (data["contents"]["twoColumnSearchResultsRenderer"]
                    ["primaryContents"]["sectionListRenderer"]["contents"])
    except (KeyError, IndexError, TypeError):
        return items
    for sec in contents:
        for vr in (sec.get("itemSectionRenderer") or {}).get("contents", []):
            v = vr.get("videoRenderer")
            if not v: continue
            try:
                items.append({
                    "id": v["videoId"],
                    "url": f"https://www.youtube.com/watch?v={v['videoId']}",
                    "title": v["title"]["runs"][0]["text"],
                    "channel": (v.get("ownerText", {}).get("runs") or [{}])[0].get("text", ""),
                    "duration": _parse_duration(v.get("lengthText", {}).get("simpleText", "")),
                    "view_count": _parse_views(v.get("viewCountText", {}).get("simpleText", "")),
                    "thumbnail": (v.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url"),
                })
            except (KeyError, IndexError):
                continue
            if len(items) >= limit: break
        if len(items) >= limit: break
    return items

def _yt_player(video_id: str) -> dict:
    last_err = None
    for c in CLIENTS:
        if c["name"] == "WEB_REMIX": continue
        body = {"videoId": video_id, "context": c["ctx"],
                "contentCheckOk": True, "racyCheckOk": True}
        try:
            r = requests.post(
                f"https://www.youtube.com/youtubei/v1/player?key={INNERTUBE_KEY}&prettyPrint=false",
                json=body, timeout=20,
                headers={"User-Agent": c["ua"], "Content-Type": "application/json",
                         "X-YouTube-Client-Name": c["name"],
                         "X-YouTube-Client-Version": c["ver"],
                         "Origin": "https://www.youtube.com",
                         "Referer": "https://www.youtube.com/"},
            )
            d = r.json()
            if d.get("streamingData"): return d
            last_err = d.get("playabilityStatus", {}).get("reason") or "no streamingData"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"YouTube player failed: {last_err}")

def _pick_formats(player: dict):
    sd = player.get("streamingData", {})
    adapt = (sd.get("adaptiveFormats") or []) + (sd.get("formats") or [])
    audio, video = [], []
    for f in adapt:
        mime = f.get("mimeType", "")
        url  = f.get("url")
        if not url: continue
        rec = {
            "format_id": f.get("itag"),
            "url":       url,
            "ext":       mime.split(";")[0].split("/")[-1],
            "mime":      mime.split(";")[0],
            "abr":       int(f.get("bitrate", 0)) // 1000 if f.get("bitrate") else 0,
            "height":    f.get("height") or 0,
            "width":     f.get("width") or 0,
            "filesize":  int(f.get("contentLength") or 0),
            "quality":   f.get("qualityLabel") or f.get("audioQuality") or "",
        }
        if mime.startswith("audio/"):   audio.append(rec)
        elif mime.startswith("video/"): video.append(rec)
    audio.sort(key=lambda x: x["abr"], reverse=True)
    video.sort(key=lambda x: x["height"], reverse=True)
    return audio, video

# ---------------------------------------------------------------------------
# Vimeo config  (mirrors p_web parseMasterJSON)
# ---------------------------------------------------------------------------
def _vimeo_config(vid: str) -> dict:
    url = f"https://player.vimeo.com/video/{vid}/config"
    r = requests.get(url, headers={"User-Agent": UA_WEB, "Referer": "https://vimeo.com/"},
                     timeout=(5, 20))
    if r.status_code == 403:
        try:
            jwt_r = requests.get("https://vimeo.com/_rv/jwt", timeout=10,
                                 headers={"User-Agent": UA_WEB})
            if jwt_r.ok:
                jwt = jwt_r.json().get("token")
                r = requests.get(url, timeout=(5, 20), headers={
                    "User-Agent": UA_WEB,
                    "Referer": "https://vimeo.com/",
                    "Authorization": f"jwt {jwt}",
                })
        except Exception:
            pass
    r.raise_for_status()
    return r.json()

def _vimeo_extract(cfg: dict, vid: str) -> dict:
    req   = cfg.get("request", {}) or {}
    files = req.get("files", {}) or {}
    v     = cfg.get("video", {}) or {}
    progressive = files.get("progressive", []) or []
    hls  = files.get("hls",  {}) or {}
    dash = files.get("dash", {}) or {}

    formats = []
    for p in progressive:
        formats.append({
            "format_id": f"prog-{p.get('height','?')}p",
            "url":       p.get("url"),
            "ext":       "mp4", "mime": "video/mp4",
            "height":    p.get("height", 0), "width": p.get("width", 0),
            "abr": 0, "filesize": 0,
            "quality": f"{p.get('height','?')}p",
        })
    for d in (dash.get("cdns") or {}).values():
        if d.get("url"):
            formats.append({"format_id": "dash", "url": d["url"], "ext": "mpd",
                            "mime": "application/dash+xml", "height": 0,
                            "width": 0, "abr": 0, "filesize": 0, "quality": "DASH"})
    for h in (hls.get("cdns") or {}).values():
        if h.get("url"):
            formats.append({"format_id": "hls", "url": h["url"], "ext": "m3u8",
                            "mime": "application/vnd.apple.mpegurl", "height": 0,
                            "width": 0, "abr": 0, "filesize": 0, "quality": "HLS"})
    formats.sort(key=lambda f: f["height"], reverse=True)
    stream = next((f["url"] for f in formats if f["ext"] == "mp4"),
                  formats[0]["url"] if formats else None)
    return {
        "video": {
            "id": vid, "title": v.get("title") or "Vimeo video",
            "duration": int(v.get("duration") or 0),
            "channel": (v.get("owner") or {}).get("name"),
            "thumbnail": (v.get("thumbs") or {}).get("1280") or (v.get("thumbs") or {}).get("640"),
        },
        "stream_url": stream,
        "formats":    formats,
    }

# ---------------------------------------------------------------------------
# Routes — search
# ---------------------------------------------------------------------------
@app.post("/api/search")
def api_search():
    b = request.get_json(force=True) or {}
    q = (b.get("query") or "").strip()
    limit = int(b.get("limit") or 10)
    if not q: return jsonify({"results": []})

    # direct youtube url
    if re.search(r"youtu\.?be|youtube\.com", q):
        vid = _video_id(q)
        if vid:
            try:
                pl = _yt_player(vid)
                vd = pl.get("videoDetails", {})
                return jsonify({"results": [{
                    "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": vd.get("title"), "channel": vd.get("author"),
                    "duration": int(vd.get("lengthSeconds") or 0),
                    "view_count": int(vd.get("viewCount") or 0),
                    "thumbnail": (vd.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url"),
                }]})
            except Exception as e:
                return jsonify({"results": [], "error": str(e)}), 500

    # direct vimeo url
    if re.search(r"vimeo\.com", q):
        vid = _vimeo_id(q)
        if vid:
            try:
                cfg = _vimeo_config(vid)
                v = _vimeo_extract(cfg, vid)["video"]
                v["url"] = f"https://vimeo.com/{vid}"
                v["view_count"] = 0
                return jsonify({"results": [v]})
            except Exception as e:
                return jsonify({"results": [], "error": str(e)}), 500

    # youtube music search
    try:
        return jsonify({"results": _ytm_search(q, limit)})
    except Exception as e:
        return jsonify({"results": [], "error": f"search failed: {e}"}), 200

# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------
@app.post("/api/resolve")
def api_resolve():
    b = request.get_json(force=True) or {}
    url = (b.get("url") or "").strip()
    if not url: return jsonify({"error": "url required"}), 400

    try:
        if re.search(r"vimeo\.com", url):
            vid = _vimeo_id(url)
            if not vid: return jsonify({"error": "bad vimeo url"}), 400
            data = _vimeo_extract(_vimeo_config(vid), vid)
            if data["stream_url"]:
                data["stream_url"] = _proxy(data["stream_url"])
            for f in data["formats"]:
                if f.get("url"): f["url"] = _proxy(f["url"])
            return jsonify(data)

        vid = _video_id(url)
        if not vid: return jsonify({"error": "unsupported url"}), 400
        pl = _yt_player(vid)
        vd = pl.get("videoDetails", {})
        audio, video = _pick_formats(pl)
        best_a = audio[0] if audio else None
        best_v = video[0] if video else None

        formats = []
        for a in audio:
            formats.append({**a, "url": _proxy(a["url"]),
                            "quality": f"{a['abr']}kbps · {a['ext']}"})
        for v in video:
            formats.append({**v, "url": _proxy(v["url"]),
                            "quality": f"{v['height']}p · {v['ext']}"})

        return jsonify({
            "video": {
                "id": vid, "title": vd.get("title"),
                "duration": int(vd.get("lengthSeconds") or 0),
                "channel": vd.get("author"),
                "thumbnail": (vd.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url"),
            },
            "stream_url": _proxy((best_v or best_a or {}).get("url", "")),
            "audio_url":  _proxy(best_a["url"]) if best_a else None,
            "video_url":  _proxy(best_v["url"]) if best_v else None,
            "formats":    formats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Lyrics
# ---------------------------------------------------------------------------
@app.get("/api/lyrics")
def api_lyrics():
    title  = request.args.get("track_name") or request.args.get("title") or ""
    artist = request.args.get("artist_name") or request.args.get("artist") or ""
    q      = request.args.get("q") or ""
    h = {"User-Agent": UA_WEB}
    try:
        if title and artist:
            r = requests.get("https://lrclib.net/api/get",
                             params={"track_name": title, "artist_name": artist},
                             timeout=15, headers=h)
            if r.ok:
                d = r.json()
                return jsonify({"lrc": d.get("syncedLyrics") or d.get("plainLyrics") or "",
                                "plain": d.get("plainLyrics") or ""})
        r = requests.get("https://lrclib.net/api/search",
                         params={"q": q or f"{title} {artist}".strip()},
                         timeout=15, headers=h)
        items = r.json() if r.ok else []
        if items:
            d = items[0]
            return jsonify({"lrc": d.get("syncedLyrics") or "", "plain": d.get("plainLyrics") or ""})
        return jsonify({"lrc": "", "plain": ""})
    except Exception as e:
        return jsonify({"error": str(e), "lrc": "", "plain": ""}), 500

# ---------------------------------------------------------------------------
# Range-aware proxy + HLS rewriter
# ---------------------------------------------------------------------------
@app.get("/api/proxy")
def api_proxy():
    target = request.args.get("url")
    if not target: return "missing url", 400
    h = {"User-Agent": UA_MOBILE, "Referer": "https://www.youtube.com/"}
    if request.headers.get("Range"): h["Range"] = request.headers["Range"]
    try:
        up = requests.get(target, headers=h, stream=True, timeout=30)
    except Exception as e:
        return str(e), 502
    def gen():
        try:
            for chunk in up.iter_content(64*1024):
                if chunk: yield chunk
        finally:
            up.close()
    out = {}
    for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if up.headers.get(k): out[k] = up.headers[k]
    if "Content-Type" not in out:
        out["Content-Type"] = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return Response(stream_with_context(gen()), status=up.status_code, headers=out)

@app.get("/api/hls")
def api_hls():
    target = request.args.get("url")
    if not target: return "missing url", 400
    try:
        r = requests.get(target, headers={"User-Agent": UA_MOBILE}, timeout=20)
        txt = r.text
    except Exception as e:
        return str(e), 502
    base = target.rsplit("/", 1)[0] + "/"
    out = []
    for line in txt.splitlines():
        if line and not line.startswith("#"):
            out.append(_proxy(urllib.parse.urljoin(base, line)))
        else:
            out.append(line)
    return Response("\n".join(out), mimetype="application/vnd.apple.mpegurl")

# ---------------------------------------------------------------------------
# Background download (server-side GET of the resolved URL → file)
# ---------------------------------------------------------------------------
@app.post("/api/download")
def api_download():
    b = request.get_json(force=True) or {}
    url = (b.get("url") or "").strip()
    title = (b.get("title") or "track").strip()
    mode = (b.get("mode") or "mp3").lower()
    if not url: return jsonify({"error": "url required"}), 400

    job_id = f"job_{int(time.time()*1000)}"
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "url": url, "title": title, "mode": mode,
                        "status": "queued", "progress": 0, "speed": "",
                        "created": time.time(), "file": None, "error": None}
    threading.Thread(target=_run_download,
                     args=(job_id, url, title, mode), daemon=True).start()
    return jsonify({"job": JOBS[job_id]})

def _run_download(job_id, page_url, title, mode):
    """
    Resolve page_url → get a direct media URL (YT/Vimeo) → stream to disk.
    For audio mode, ask resolve for the best audio format; for video, best video.
    """
    try:
        with JOBS_LOCK: JOBS[job_id]["status"] = "starting"

        # resolve first
        if re.search(r"vimeo\.com", page_url):
            vid = _vimeo_id(page_url)
            data = _vimeo_extract(_vimeo_config(vid), vid)
            direct = data["stream_url"]
            ext = "mp4"
        else:
            v = _video_id(page_url)
            if not v: raise RuntimeError("unsupported url")
            pl = _yt_player(v)
            audio, video = _pick_formats(pl)
            if mode == "mp3" and audio:
                direct = audio[0]["url"]; ext = "m4a"
            elif video:
                direct = video[0]["url"]; ext = "mp4"
            elif audio:
                direct = audio[0]["url"]; ext = "m4a"
            else:
                raise RuntimeError("no formats")

        safe = re.sub(r'[^\w\-. ]', "_", title)[:120] or "track"
        dest = DOWNLOAD_DIR / f"{safe}.{ext}"

        with JOBS_LOCK: JOBS[job_id]["status"] = "downloading"
        h = {"User-Agent": UA_MOBILE}
        with requests.get(direct, headers=h, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0; last = time.time()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(128*1024):
                    if not chunk: continue
                    f.write(chunk); done += len(chunk)
                    if time.time() - last > 0.4:
                        with JOBS_LOCK:
                            j = JOBS[job_id]
                            j["progress"] = round(done/total*100, 1) if total else 0
                            j["speed"] = f"{done//1024} KB"
                        last = time.time()

        with JOBS_LOCK:
            j = JOBS[job_id]; j["status"] = "done"
            j["progress"] = 100; j["file"] = dest.name
    except Exception as e:
        with JOBS_LOCK:
            j = JOBS[job_id]; j["status"] = "error"; j["error"] = str(e)

# ---------------------------------------------------------------------------
# Jobs endpoints
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
def api_jobs():
    with JOBS_LOCK: return jsonify({"jobs": list(JOBS.values())})

@app.get("/api/jobs/<jid>")
def api_job(jid):
    with JOBS_LOCK: j = JOBS.get(jid)
    return jsonify(j) if j else (jsonify({"error": "not found"}), 404)

@app.post("/api/jobs/<jid>/cancel")
def api_cancel(jid):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j and j["status"] in ("queued","downloading"): j["status"] = "cancelled"
    return jsonify({"ok": True})

@app.post("/api/jobs/<jid>/retry")
def api_retry(jid):
    with JOBS_LOCK: j = JOBS.get(jid)
    if not j: return jsonify({"error": "not found"}), 404
    with JOBS_LOCK:
        j["status"] = "queued"; j["progress"] = 0; j["error"] = None
    threading.Thread(target=_run_download,
                     args=(jid, j["url"], j["title"], j.get("mode","mp3")),
                     daemon=True).start()
    return jsonify({"ok": True})

@app.delete("/api/jobs/<jid>")
def api_delete(jid):
    with JOBS_LOCK: JOBS.pop(jid, None)
    return jsonify({"ok": True})

@app.get("/api/files/<path:name>")
def api_file(name):
    p = (DOWNLOAD_DIR / name).resolve()
    if not str(p).startswith(str(DOWNLOAD_DIR)) or not p.is_file():
        return "not found", 404
    return send_file(p, as_attachment=True, download_name=p.name)

@app.get("/api/files")
def api_files():
    """List downloaded files (the bridge's 'folder' endpoint)."""
    items = []
    for p in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file(): continue
        st = p.stat()
        items.append({
            "title":   p.name,
            "path":    str(p),
            "uri":     "/api/files/" + p.name,
            "sizeMb":  f"{st.st_size/1e6:.2f} MB",
            "time":    int(st.st_mtime * 1000),
            "type":    "video" if p.suffix.lower() in (".mp4",".webm",".mkv") else "audio",
        })
    return jsonify({"folder": str(DOWNLOAD_DIR), "count": len(items), "items": items})

@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "engines": ["youtubei", "vimeo", "lrclib"]})

# ---------------------------------------------------------------------------
# Serve HTML
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/<path:p>")
def static_files(p):
    return send_from_directory(BASE_DIR, p)

if __name__ == "__main__":
    print(f"→ http://127.0.0.1:{PORT}")
    print(f"→ downloads: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
