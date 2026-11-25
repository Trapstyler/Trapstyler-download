#!/usr/bin/env python3
"""
Musick backend — endpoint-only (youtubei / vimeo config / lrclib). No yt-dlp.
Run:  python app.py
Deps: pip install flask requests
"""
import os, re, json, time, threading, urllib.parse, mimetypes
from pathlib import Path

import requests
from flask import Flask, request, Response, jsonify, send_file, stream_with_context, send_from_directory

PORT         = int(os.environ.get("PORT", 8000))
BASE_DIR     = Path(__file__).parent.resolve()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads"))).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

UA_MOBILE  = "com.google.android.youtube/19.29.37 (Linux; U; Android 11) gzip"
UA_ANDROID_VR = "com.google.android.apps.youtube.vr.oculus/1.56.21 (Linux; U; Android 12L; Quest 3 Build/SQ3A.220605.009.A1) gzip"
UA_IOS     = "com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"
UA_WEB     = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# Player-resolution clients only. Do not add WEB or ANDROID here.
VISIONOS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0.0.0 Safari/537.36")

CLIENTS = [
    {
        "name": "VISIONOS",
        "id": "101",
        "ver": "1.0.0",
        "ua": VISIONOS_UA,
        "ctx": {"client": {
            "clientName": "VISIONOS",
            "clientVersion": "1.0.0",
            "hl": "en",
            "gl": "US",
        }},
    },
    {
        "name": "ANDROID_VR",
        "id": "28",
        "ver": "1.60.19",
        "ua": ("com.google.android.apps.youtube.vr/1.60.19 "
               "(Linux; U; Android 12L; Quest 3 Build/SQ3A.220605.009.A1) gzip"),
        "ctx": {"client": {
            "clientName": "ANDROID_VR",
            "clientVersion": "1.60.19",
            "deviceMake": "Oculus",
            "deviceModel": "Quest 3",
            "osName": "Android",
            "osVersion": "12L",
            "androidSdkVersion": 32,
            "hl": "en",
            "gl": "US",
        }},
    },
]

# Search-only clients remain available for YouTube Music search. They are NOT
# used by _yt_player, so a WEB 403 cannot block media resolution.
SEARCH_CLIENTS = {
    "WEB": {
        "name": "WEB", "id": "1", "ver": "2.20240726.00.00", "ua": UA_WEB,
        "ctx": {"client": {"clientName": "WEB", "clientVersion": "2.20240726.00.00",
                           "hl": "en", "gl": "US"}},
    },
    "WEB_REMIX": {
        "name": "WEB_REMIX", "id": "67", "ver": "1.20250317.01.00", "ua": UA_WEB,
        "ctx": {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20250317.01.00",
                           "platform": "DESKTOP", "hl": "en", "gl": "US"}},
    },
}

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

def _upgrade_thumb(raw_url: str, vid: str = None) -> str:
    """Request high-resolution YouTube artwork; callers can fall back if unavailable."""
    if raw_url:
        if "googleusercontent.com" in raw_url:
            return re.sub(r"=w\d+-h\d+([^\&\s]*)", r"=w800-h800\1", raw_url)
        upgraded = re.sub(
            r"/(default|mqdefault|sddefault|hqdefault)\.(jpg|webp)",
            r"/maxresdefault.\2", raw_url
        )
        if upgraded != raw_url:
            return upgraded
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    return raw_url or ""

# ---------------------------------------------------------------------------
# YouTube Music search
# ---------------------------------------------------------------------------
def _ytm_search(query: str, limit: int) -> list:
    web_remix = SEARCH_CLIENTS["WEB_REMIX"]
    body = {
        "query":   query,
        "params":  "Eg-KAQwIARAAGAAgACgAMABqChAEEAUQAxAKEAk%3D",
        "context": web_remix["ctx"],
    }
    url = f"https://music.youtube.com/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false"
    headers = {"User-Agent": web_remix["ua"], "Content-Type": "application/json",
               "X-YouTube-Client-Name": web_remix["id"],
               "X-YouTube-Client-Version": web_remix["ver"],
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
    if r is None or not r.text.strip():
        return _yt_plain_search(query, limit)

    try:
        data = r.json()
    except Exception:
        return _yt_plain_search(query, limit)

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
            thumbs = r0["thumbnail"]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"]
            raw_cover = thumbs[-1]["url"] if thumbs else ""
            vid   = r0["playlistItemData"]["videoId"]
            cover = _upgrade_thumb(raw_cover, vid)
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

def _yt_plain_search(query: str, limit: int) -> list:
    """Fallback: plain youtubei search with WEB client."""
    web_client = SEARCH_CLIENTS["WEB"]
    body = {"query": query, "context": web_client["ctx"]}
    r = requests.post(
        f"https://www.youtube.com/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false",
        json=body, timeout=(5, 20),
        headers={"User-Agent": web_client["ua"], "Content-Type": "application/json",
                 "X-YouTube-Client-Name": web_client["id"],
                 "X-YouTube-Client-Version": web_client["ver"],
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
                vid = v["videoId"]
                raw_thumb = (v.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url", "")
                items.append({
                    "id": vid,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": v["title"]["runs"][0]["text"],
                    "channel": (v.get("ownerText", {}).get("runs") or [{}])[0].get("text", ""),
                    "duration": _parse_duration(v.get("lengthText", {}).get("simpleText", "")),
                    "view_count": _parse_views(v.get("viewCountText", {}).get("simpleText", "")),
                    "thumbnail": _upgrade_thumb(raw_thumb, vid),
                })
            except (KeyError, IndexError):
                continue
            if len(items) >= limit: break
        if len(items) >= limit: break
    return items

# ---------------------------------------------------------------------------
# YouTube player (Fixed numeric Client-Name IDs + safe JSON parsing)
# ---------------------------------------------------------------------------
def _yt_player(video_id: str) -> dict:
    """Resolve direct YouTube media URLs using VISIONOS, then ANDROID_VR."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")):
        raise ValueError("Invalid YouTube video ID")

    endpoint = (
        "https://www.youtube.com/youtubei/v1/player"
        f"?key={INNERTUBE_KEY}&prettyPrint=false"
    )
    errors = []

    for client in CLIENTS:  # VISIONOS first, ANDROID_VR second
        headers = {
            "User-Agent": client["ua"],
            "X-YouTube-Client-Name": client["id"],
            "X-YouTube-Client-Version": client["ver"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "context": client["ctx"],
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=(7, 20),
            )

            if response.status_code != 200:
                errors.append(
                    f"{client['name']}: HTTP {response.status_code} "
                    f"({response.headers.get('Content-Type', 'unknown content type')})"
                )
                continue

            if not response.content or not response.text.strip():
                errors.append(f"{client['name']}: empty response")
                continue

            try:
                data = response.json()
            except ValueError:
                errors.append(
                    f"{client['name']}: non-JSON response "
                    f"({response.headers.get('Content-Type', 'unknown content type')})"
                )
                continue

            if not isinstance(data, dict):
                errors.append(f"{client['name']}: invalid JSON response")
                continue

            streaming = data.get("streamingData") or {}
            # Use only direct URLs supplied by YouTube. No signatureCipher
            # deciphering is attempted here.
            direct_formats = [
                item for item in (
                    (streaming.get("adaptiveFormats") or [])
                    + (streaming.get("formats") or [])
                )
                if isinstance(item, dict) and item.get("url")
            ]

            if direct_formats:
                return data

            playability = data.get("playabilityStatus") or {}
            reason = (
                playability.get("reason")
                or playability.get("status")
                or "streamingData contained no direct media URLs"
            )
            errors.append(f"{client['name']}: {reason}")

        except requests.Timeout:
            errors.append(f"{client['name']}: request timed out")
        except requests.RequestException as exc:
            errors.append(f"{client['name']}: network error ({exc})")

    raise RuntimeError(
        "YouTube player failed: " + "; ".join(errors or ["no response"])
    )

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
# Vimeo config
# ---------------------------------------------------------------------------
def _vimeo_config(vid: str) -> dict:
    """Fetch Vimeo player config, retrying a 403 with Vimeo's JWT."""
    url = f"https://player.vimeo.com/video/{vid}/config"
    headers = {
        "User-Agent": UA_WEB,
        "Referer": "https://vimeo.com/",
        "Accept": "application/json, text/plain, */*",
    }

    response = requests.get(url, headers=headers, timeout=(6, 20))
    if response.status_code == 403:
        jwt_response = requests.get(
            "https://vimeo.com/_rv/jwt",
            headers={"User-Agent": UA_WEB, "Referer": "https://vimeo.com/"},
            timeout=(6, 15),
        )
        jwt_response.raise_for_status()
        try:
            jwt_data = jwt_response.json()
            token = jwt_data.get("token") or jwt_data.get("jwt")
        except ValueError:
            token = jwt_response.text.strip().strip('"')

        if not token:
            raise RuntimeError("Vimeo returned 403 and no JWT token was available")

        retry_headers = dict(headers)
        retry_headers["Authorization"] = f"jwt {token}"
        response = requests.get(url, headers=retry_headers, timeout=(6, 20))

    response.raise_for_status()
    return response.json()

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
                raw_t = (vd.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url", "")
                return jsonify({"results": [{
                    "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": vd.get("title"), "channel": vd.get("author"),
                    "duration": int(vd.get("lengthSeconds") or 0),
                    "view_count": int(vd.get("viewCount") or 0),
                    "thumbnail": _upgrade_thumb(raw_t, vid),
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
        raw_t = (vd.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url", "")

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
                "thumbnail": _upgrade_thumb(raw_t, vid),
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
    title = (request.args.get("track_name") or request.args.get("title") or "").strip()
    artist = (request.args.get("artist_name") or request.args.get("artist") or "").strip()
    query = (request.args.get("q") or "").strip()

    headers = {
        "User-Agent": UA_WEB,
        "Accept": "application/json",
    }

    try:
        # Exact metadata lookup first.
        if title and artist:
            response = requests.get(
                "https://lrclib.net/api/get",
                params={"track_name": title, "artist_name": artist},
                headers=headers,
                timeout=(5, 15),
            )
            if response.status_code == 200:
                data = response.json()
                synced = data.get("syncedLyrics") or ""
                plain = data.get("plainLyrics") or ""
                if synced or plain:
                    return jsonify({"lrc": synced, "plain": plain})

        # Search fallback is useful for remixes, alternate spellings and
        # tracks whose artist/title metadata differs from the source listing.
        search_query = query or f"{title} {artist}".strip()
        if search_query:
            response = requests.get(
                "https://lrclib.net/api/search",
                params={"q": search_query},
                headers=headers,
                timeout=(5, 15),
            )
            response.raise_for_status()
            results = response.json()
            if isinstance(results, list) and results:
                # Prefer a result with synchronized lyrics, then plain lyrics.
                best = next(
                    (item for item in results if item.get("syncedLyrics")),
                    next((item for item in results if item.get("plainLyrics")), results[0]),
                )
                return jsonify({
                    "lrc": best.get("syncedLyrics") or "",
                    "plain": best.get("plainLyrics") or "",
                })

        return jsonify({"lrc": "", "plain": ""})

    except requests.RequestException as exc:
        return jsonify({"error": f"Lyrics service request failed: {exc}",
                        "lrc": "", "plain": ""}), 502
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"Invalid lyrics response: {exc}",
                        "lrc": "", "plain": ""}), 502

# ---------------------------------------------------------------------------
# Range-aware proxy + HLS rewriter
# ---------------------------------------------------------------------------
@app.get("/api/proxy")
def api_proxy():
    target = request.args.get("url")
    if not target: return "missing url", 400
    h = {"User-Agent": UA_ANDROID_VR}
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
# Background download
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
    try:
        with JOBS_LOCK: JOBS[job_id]["status"] = "starting"

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
        h = {"User-Agent": UA_ANDROID_VR}
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
# Embedded frontend (single-file deployment)
# ---------------------------------------------------------------------------
INDEX_HTML = r'''<!doctype html>
<html lang="en" data-theme="dark" data-density="comfortable">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1a1a2e">
<title>Musick · Musick AI</title>
<style>
*, *::before, *::after { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; min-height: 100%; }

:root {
  --bg: #1a1a2e;
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
  --raised: 6px 6px 14px rgba(0, 0, 0, 0.55), -6px -6px 14px rgba(255, 255, 255, 0.03);
  --raised-sm: 3px 3px 8px rgba(0, 0, 0, 0.55), -3px -3px 8px rgba(255, 255, 255, 0.03);
  --inset: inset 4px 4px 10px rgba(0, 0, 0, 0.55), inset -4px -4px 10px rgba(255, 255, 255, 0.03);
  --inset-sm: inset 2px 2px 6px rgba(0, 0, 0, 0.55), inset -2px -2px 6px rgba(255, 255, 255, 0.03);
  --radius: 16px;
  --radius-sm: 10px;
  --radius-lg: 20px;
}

[data-theme="light"] {
  --bg: #e8eaf0;
  --surface: #eef0f6;
  --surface-2: #f5f7fc;
  --surface-3: #ffffff;
  --text: #1a1d2e;
  --text-dim: #6a6d80;
  --text-faint: #a0a3b5;
  --raised: 6px 6px 14px rgba(170, 175, 195, 0.55), -6px -6px 14px rgba(255, 255, 255, 0.95);
  --raised-sm: 3px 3px 8px rgba(170, 175, 195, 0.55), -3px -3px 8px rgba(255, 255, 255, 0.95);
  --inset: inset 4px 4px 10px rgba(170, 175, 195, 0.55), inset -4px -4px 10px rgba(255, 255, 255, 0.95);
  --inset-sm: inset 2px 2px 6px rgba(170, 175, 195, 0.55), inset -2px -2px 6px rgba(255, 255, 255, 0.95);
}

[data-density="compact"] { font-size: 13px; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  padding-bottom: 90px;
}

button { font-family: inherit; font-size: inherit; cursor: pointer; border: none; background: none; color: inherit; outline: none; }
button:active { transform: scale(0.97); }
input, select, textarea { font-family: inherit; font-size: inherit; color: inherit; }

.icon {
  width: 1.2em; height: 1.2em; flex-shrink: 0;
  fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round;
  vertical-align: -0.2em;
}

/* 1. Topbar */
.topbar {
  position: sticky; top: 0; z-index: 40;
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg);
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
[data-theme="light"] .topbar { border-bottom-color: rgba(0, 0, 0, 0.06); }
.brand { display: flex; align-items: center; gap: 10px; }
.brand-icon { width: 24px; height: 24px; color: var(--accent); stroke-width: 2.4; }
.brand-name { font-weight: 700; font-size: 18px; letter-spacing: 0.3px; }
.topbar-actions { display: flex; align-items: center; gap: 8px; }

.health-pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 10px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--raised-sm);
  font-size: 11px; font-weight: 700; color: var(--text-dim);
}
.health-pill .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-faint);
}
.health-pill.ok .dot { background: var(--success); box-shadow: 0 0 6px var(--success); }
.health-pill.ok { color: var(--success); }
.health-pill.bad .dot { background: var(--danger); }
.health-pill.bad { color: var(--danger); }

.icon-btn {
  width: 36px; height: 36px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--surface); box-shadow: var(--raised-sm);
  color: var(--text);
}
.icon-btn svg { width: 20px; height: 20px; }
.icon-btn:active { box-shadow: var(--inset-sm); }

/* Main container */
.main {
  max-width: 900px;
  margin: 0 auto;
  padding: 16px 16px 80px;
}
section { margin-bottom: 24px; }
.card-box {
  background: var(--surface);
  box-shadow: var(--raised);
  border-radius: var(--radius);
  padding: 18px;
}

/* 2. Hero / Search */
.hero-title { margin: 0 0 2px; font-size: 24px; font-weight: 800; letter-spacing: -0.3px; }
.hero-sub { margin: 0 0 14px; font-size: 13px; color: var(--text-dim); }

.search-row { display: flex; gap: 8px; align-items: stretch; flex-wrap: wrap; }
.search-input-wrap {
  flex: 1; display: flex; align-items: center; gap: 8px;
  padding: 0 14px;
  border-radius: 999px;
  background: var(--surface);
  box-shadow: var(--inset);
  min-width: 200px;
}
.search-input-wrap .search-icon { color: var(--text-dim); }
#searchInput {
  flex: 1; min-width: 0;
  background: transparent; border: none; outline: none;
  padding: 12px 0; color: var(--text);
}
#searchInput::placeholder { color: var(--text-faint); }

.btn-primary {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 16px; border-radius: var(--radius-sm);
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  color: #fff; font-weight: 600;
  box-shadow: 4px 4px 12px rgba(77, 107, 254, 0.32);
  white-space: nowrap;
}
.btn-primary.loading { opacity: 0.75; pointer-events: none; }

.btn-secondary {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 14px; border-radius: var(--radius-sm);
  background: var(--surface); color: var(--text);
  font-weight: 600; box-shadow: var(--raised-sm);
  white-space: nowrap;
}
.btn-secondary:active { box-shadow: var(--inset-sm); }

.btn-text {
  color: var(--text-dim); font-size: 12px; font-weight: 600;
  padding: 4px 8px; border-radius: 6px;
}

.mode-row { margin-top: 14px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
.segmented {
  display: inline-flex; padding: 4px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--inset-sm); gap: 4px;
}
.seg-btn {
  padding: 6px 16px; border-radius: 999px;
  font-weight: 600; font-size: 12px; color: var(--text-dim);
}
.seg-btn.active {
  background: var(--surface-3); color: var(--text); box-shadow: var(--raised-sm);
}

.chips-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }
.chips-label { font-size: 11.5px; font-weight: 700; color: var(--text-dim); margin-right: 2px; }
.chip {
  padding: 6px 12px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--raised-sm);
  color: var(--text-dim); font-size: 12px; font-weight: 600;
  transition: color 0.15s;
}
.chip:hover, .chip:active { box-shadow: var(--inset-sm); color: var(--accent); }
.chip.recent-chip { border: 1px solid rgba(77, 107, 254, 0.25); color: var(--text); }

/* 3. Controls row */
.controls-row {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-top: 14px; padding-top: 14px;
  border-top: 1px solid rgba(255, 255, 255, 0.04);
}
[data-theme="light"] .controls-row { border-top-color: rgba(0, 0, 0, 0.06); }

.select-control {
  padding: 8px 26px 8px 10px; border-radius: var(--radius-sm);
  background: var(--surface); border: none; outline: none;
  box-shadow: var(--inset-sm); color: var(--text); font-size: 12px;
  appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238a8aa0' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
  background-repeat: no-repeat;
  background-position: right 8px center;
}
.select-control[hidden] { display: none; }

/* 4. Results */
.section-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 12px; gap: 8px;
}
.section-head h2 { margin: 0; font-size: 16px; font-weight: 700; }
.empty-state {
  text-align: center; padding: 28px 16px;
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius); color: var(--text-dim); font-size: 13px;
}

.results-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px;
}
.result-card {
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius); padding: 12px;
  display: flex; flex-direction: column; gap: 8px;
}
.result-thumb {
  aspect-ratio: 16/9; border-radius: 10px; overflow: hidden;
  position: relative; background: var(--surface-2); cursor: pointer;
}
.result-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; image-rendering: auto; backface-visibility: hidden; }
.duration-badge {
  position: absolute; bottom: 6px; right: 6px;
  background: rgba(0, 0, 0, 0.82); color: #fff;
  font-size: 11px; padding: 2px 6px; border-radius: 6px; font-weight: 600;
}
.result-title {
  font-size: 13.5px; font-weight: 600; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; cursor: pointer;
}
.result-meta {
  font-size: 11.5px; color: var(--text-dim);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.result-actions { display: flex; gap: 8px; margin-top: auto; padding-top: 4px; }
.action-pill {
  flex: 1; padding: 7px 10px; border-radius: var(--radius-sm);
  background: var(--surface); box-shadow: var(--raised-sm);
  font-size: 12px; font-weight: 600;
  display: inline-flex; align-items: center; justify-content: center; gap: 4px;
}
.action-pill:active { box-shadow: var(--inset-sm); }

.result-progress {
  height: 5px; background: var(--surface-2);
  border-radius: 3px; overflow: hidden; display: none;
}
.result-progress-fill {
  height: 100%; width: 0%; background: var(--accent); transition: width 0.3s ease;
}
.result-progress-fill.done { background: var(--success); }
.result-progress-fill.error { background: var(--danger); }

/* 5. Downloads / Jobs */
.filter-tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.filter-tab {
  padding: 6px 14px; border-radius: 999px;
  background: var(--surface); box-shadow: var(--raised-sm);
  font-size: 12px; font-weight: 600; color: var(--text-dim);
}
.filter-tab.active { background: var(--accent); color: #fff; }

.jobs-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
.job-card {
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius); padding: 14px;
  display: flex; flex-direction: column; gap: 8px;
}
.job-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.job-title {
  font-weight: 600; font-size: 13.5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;
}
.job-status {
  font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 999px;
  text-transform: uppercase; background: var(--surface-2); flex-shrink: 0;
}
.status-queued, .status-starting { color: var(--text-dim); }
.status-downloading, .status-processing { color: var(--accent); }
.status-done { color: var(--success); }
.status-error, .status-cancelled { color: var(--danger); }

.job-meta { display: flex; justify-content: space-between; font-size: 11.5px; color: var(--text-dim); }
.progress-bar {
  height: 6px; background: var(--surface-2);
  border-radius: 3px; overflow: hidden; box-shadow: var(--inset-sm);
}
.progress-fill { height: 100%; width: 0%; background: var(--accent); transition: width 0.3s ease; }
.progress-fill.done { background: var(--success); }
.progress-fill.error { background: var(--danger); }
.job-error { font-size: 11.5px; color: var(--danger); }
.job-actions { display: flex; gap: 8px; flex-wrap: wrap; }

/* 6. Chat Panel */
.chat-panel {
  background: var(--surface);
  box-shadow: var(--raised);
  border-radius: var(--radius);
  display: flex; flex-direction: column;
  height: calc(100vh - 170px);
  min-height: 420px;
  overflow: hidden;
}
.chat-panel[hidden] { display: none; }
.chat-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px; background: var(--surface-2);
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.chat-title { font-weight: 700; font-size: 15px; display: flex; align-items: center; gap: 8px; }
.chat-badge {
  font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
  background: rgba(76, 175, 120, 0.18); color: var(--success);
}
.chat-body { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; }
.chat-empty {
  margin: auto; text-align: center; color: var(--text-dim);
  padding: 24px; max-width: 320px;
}
.chat-empty[hidden] { display: none; }
.chat-messages { display: flex; flex-direction: column; gap: 10px; }
.msg-row { display: flex; flex-direction: column; max-width: 84%; }
.msg-row.user { align-self: flex-end; }
.msg-row.bot { align-self: flex-start; }
.bubble {
  padding: 10px 14px; border-radius: 14px;
  font-size: 13.5px; line-height: 1.45; word-wrap: break-word; white-space: pre-wrap;
}
.msg-row.user .bubble {
  background: var(--accent); color: #fff; border-bottom-right-radius: 4px;
}
.msg-row.bot .bubble {
  background: var(--surface-2); color: var(--text);
  box-shadow: var(--raised-sm); border-bottom-left-radius: 4px;
}
.inline-results { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.inline-card {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 8px; background: var(--surface);
}
.inline-card img { width: 44px; height: 32px; object-fit: cover; border-radius: 6px; }
.inline-info { flex: 1; min-width: 0; }
.inline-title { font-size: 12px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.inline-sub { font-size: 10.5px; color: var(--text-dim); }
.inline-btns { display: flex; gap: 4px; }

.typing-indicator {
  display: none; font-size: 12px; color: var(--text-dim);
  padding: 6px 4px; font-style: italic;
}
.typing-indicator.show { display: block; }

.chat-input-row {
  position: sticky; bottom: 0; z-index: 95;
  display: flex; gap: 8px; padding: 10px 12px;
  background: var(--surface-2);
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}
#chatInput {
  flex: 1; padding: 10px 14px; border-radius: 999px;
  background: var(--surface); border: none; outline: none;
  box-shadow: var(--inset-sm); color: var(--text);
}

/* 7. Bottom Nav */
.bottom-nav {
  position: fixed; left: 0; right: 0; bottom: 0; height: 64px;
  background: var(--surface);
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  display: flex; align-items: center; justify-content: space-around;
  z-index: 90;
  padding-bottom: env(safe-area-inset-bottom);
}
[data-theme="light"] .bottom-nav { border-top-color: rgba(0, 0, 0, 0.08); }
.nav-btn {
  flex: 1; display: flex; flex-direction: column; align-items: center; gap: 3px;
  color: var(--text-dim); font-size: 11px; font-weight: 600; padding: 6px 0;
}
.nav-btn.active { color: var(--accent); }

/* 8. Modals */
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(5px);
  display: flex; align-items: center; justify-content: center;
  padding: 16px; z-index: 200;
}
.modal-backdrop[hidden] { display: none; }
.modal {
  background: var(--surface); box-shadow: var(--raised);
  border-radius: var(--radius);
  width: 100%; max-width: 520px; max-height: 88vh;
  display: flex; flex-direction: column; overflow: hidden;
}
.modal-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.modal-head h2 { margin: 0; font-size: 16px; font-weight: 700; }
.modal-body {
  padding: 16px 18px; overflow-y: auto; flex: 1;
  display: flex; flex-direction: column; gap: 12px;
}
.modal-foot {
  padding: 12px 18px; border-top: 1px solid rgba(255, 255, 255, 0.06);
  display: flex; justify-content: flex-end; gap: 8px;
}
.form-section-title {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.6px; color: var(--text-dim); margin-top: 4px;
  display: flex; align-items: center; justify-content: space-between;
}
.form-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.form-row label { font-size: 13px; color: var(--text); }
.form-row .select-control,
.form-row input[type="color"] { min-width: 150px; }

.preview-player {
  aspect-ratio: 16/9; background: #000; border-radius: 10px; overflow: hidden;
  display: flex; align-items: center; justify-content: center; color: var(--text-dim);
}
.preview-player iframe, .preview-player video, .preview-player audio {
  width: 100%; height: 100%; border: none;
}

.history-list { display: flex; flex-direction: column; gap: 6px; }
.history-item {
  padding: 8px 12px; border-radius: var(--radius-sm);
  background: var(--surface-2); font-size: 12.5px;
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
}
#batchTextarea {
  width: 100%; min-height: 150px; padding: 12px; border-radius: var(--radius-sm);
  background: var(--surface-2); border: none; outline: none;
  box-shadow: var(--inset-sm); color: var(--text); resize: vertical;
}

/* 9. Toast Container */
.toast-container {
  position: fixed; top: 16px; right: 16px; z-index: 400;
  display: flex; flex-direction: column; gap: 8px; pointer-events: none;
}
.toast {
  background: var(--surface-2); color: var(--text);
  padding: 10px 14px; border-radius: var(--radius-sm);
  box-shadow: var(--raised); font-size: 12.5px; font-weight: 600;
  opacity: 0; transform: translateY(-8px); transition: opacity 0.2s, transform 0.2s;
}
.toast.show { opacity: 1; transform: translateY(0); }
.toast-success { border-left: 3px solid var(--success); }
.toast-error { border-left: 3px solid var(--danger); }

.search-progress { height: 4px; background: rgba(255,255,255,.1); border-radius: 99px; overflow: hidden; margin: 8px 0; }
.search-progress-bar { height: 100%; width: 40%; background: var(--accent); border-radius: 99px; animation: searchSlide 1s infinite linear; }
@keyframes searchSlide { 0% { transform: translateX(-100%); } 100% { transform: translateX(300%); } }
.inline-searching, .inline-empty { opacity: .75; font-size: 13px; padding: 6px 0; }

/* Persistent Top Download Progress Bar */
#dlProgressWrap {
  position: fixed; left: 12px; right: 12px; top: 12px; z-index: 10000;
  background: var(--surface-2); border: 1px solid rgba(255,255,255,0.12);
  border-radius: 14px; padding: 10px 14px; display: none;
  box-shadow: 0 8px 30px rgba(0,0,0,.55);
}
#dlProgressWrap.show { display: block; }
#dlProgressTitle { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text); }
#dlProgressSub { font-size: 11px; opacity: .7; margin-top: 2px; color: var(--text-dim); }
#dlProgressTrack { height: 6px; background: rgba(0,0,0,0.3); border-radius: 99px; overflow: hidden; margin-top: 8px; }
#dlProgressFill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); border-radius: 99px; transition: width .25s ease; }

/* Media Player Bar */
#trapPlayerBar {
  position: fixed; left: 8px; right: 8px; bottom: 72px;
  border-radius: 16px; z-index: 9999; background: #111;
  border: 1px solid #333; padding: 12px;
  display: none; flex-direction: column; gap: 10px;
  box-shadow: 0 10px 30px rgba(0,0,0,.6);
  max-height: 82vh; overflow-y: auto;
}
#trapPlayerBar.show { display: flex; }
#trapPlayerBar video { width: 100%; max-height: 300px; display: none; border-radius: 12px; background: #000; aspect-ratio: 16/9; object-fit: contain; }
#trapPlayerBar.video-on #trapVideo { display: block; }
#trapPlayerBar.mini {
  height: 62px; max-height: 62px; overflow: hidden; border-radius: 32px;
  padding: 8px 12px; flex-direction: row; align-items: center;
}
#trapPlayerBar.mini > div:nth-child(n+2), #trapPlayerBar.mini #trapVideo { display: none !important; }
#trapPlayerBar.mini #trapMinBtn { transform: rotate(180deg); }
body.on-ai-chat #trapPlayerBar { display: none !important; }
body.player-active .main { padding-bottom: 180px !important; }
</style>
</head>
<body>

<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <symbol id="i-music" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></symbol>
  <symbol id="i-history" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></symbol>
  <symbol id="i-settings" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></symbol>
  <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></symbol>
  <symbol id="i-video" viewBox="0 0 24 24"><path fill="currentColor" d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
  <symbol id="i-home" viewBox="0 0 24 24"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
  <symbol id="i-sparkles" viewBox="0 0 24 24"><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1"/></symbol>
</svg>

<!-- 1. Topbar -->
<header class="topbar">
  <div class="brand">
    <svg class="icon brand-icon"><use href="#i-music"/></svg>
    <span class="brand-name">Musick</span>
  </div>
  <div class="topbar-actions">
    <div class="health-pill" id="healthPill" title="Backend status">
      <span class="dot"></span>
      <span id="healthText">...</span>
    </div>
    <button class="icon-btn" id="historyBtn" title="History"><svg class="icon"><use href="#i-history"/></svg></button>
    <button class="icon-btn" id="settingsBtn" title="Settings"><svg class="icon"><use href="#i-settings"/></svg></button>
    <button class="icon-btn" id="themeBtn" title="Toggle theme"><svg class="icon" id="themeIcon"><use href="#i-sun"/></svg></button>
  </div>
</header>

<main class="main">
  <!-- HOME VIEW -->
  <div id="homeView">
    <!-- 2. Hero / Search section -->
    <section class="card-box">
      <h1 class="hero-title">Musick</h1>
      <p class="hero-sub">Download music &amp; videos</p>

      <div class="search-row">
        <div class="search-input-wrap">
          <svg class="icon search-icon"><use href="#i-search"/></svg>
          <input type="text" id="searchInput" placeholder="Search songs, artists, or paste a link..." autocomplete="off" spellcheck="false">
        </div>
        <button class="btn-primary" id="searchBtn">Search</button>
        <button class="btn-secondary" id="pasteBtn">Paste</button>
      </div>

      <div class="mode-row">
        <div class="segmented" id="modeToggle">
          <button class="seg-btn active" data-mode="mp3">Audio</button>
          <button class="seg-btn" data-mode="mp4">Video</button>
        </div>
      </div>

      <!-- Recent Search History Chips -->
      <div class="chips-row" id="recentChipsRow" hidden></div>

      <!-- Genre Chips -->
      <div class="chips-row" id="chipsRow">
        <button class="chip" data-chip="Trending">Trending</button>
        <button class="chip" data-chip="Amapiano">Amapiano</button>
        <button class="chip" data-chip="Gospel">Gospel</button>
        <button class="chip" data-chip="Afrobeats">Afrobeats</button>
        <button class="chip" data-chip="Hip Hop">Hip Hop</button>
        <button class="chip" data-chip="Bongo">Bongo</button>
      </div>

      <!-- 3. Controls row -->
      <div class="controls-row">
        <select id="lengthFilter" class="select-control">
          <option value="all">All lengths</option>
          <option value="short">Short &lt;4min</option>
          <option value="medium">Medium</option>
          <option value="long">Long</option>
        </select>
        <select id="sortSelect" class="select-control">
          <option value="relevance">Relevance</option>
          <option value="newest">Newest</option>
          <option value="views">Most viewed</option>
        </select>
        <select id="qualitySelect" class="select-control" hidden>
          <option value="720" selected>720p</option>
          <option value="1080">1080p</option>
          <option value="480">480p</option>
        </select>
        <select id="bitrateSelect" class="select-control">
          <option value="128">128 kbps</option>
          <option value="192" selected>192 kbps</option>
          <option value="320">320 kbps</option>
        </select>
        <select id="countSelect" class="select-control">
          <option value="10" selected>10 results</option>
          <option value="20">20 results</option>
          <option value="30">30 results</option>
        </select>
        <button class="btn-secondary" id="batchBtn">Batch</button>
      </div>
    </section>

    <!-- 4. Results -->
    <section id="resultsSection">
      <div class="section-head">
        <h2>Results</h2>
      </div>
      <div class="empty-state" id="resultsEmpty">Search for something to see results</div>
      <div id="searchProgress" class="search-progress" hidden><div class="search-progress-bar"></div></div>
      <div class="results-grid" id="resultsGrid"></div>
    </section>
  </div>

  <!-- 5. Jobs / Downloads -->
  <section id="downloadsSection">
    <div class="section-head">
      <h2>Downloads</h2>
      <button class="btn-text" id="refreshDownloadsBtn">Refresh</button>
    </div>
    <div class="filter-tabs" id="filterTabs">
      <button class="filter-tab active" data-filter="all">All</button>
      <button class="filter-tab" data-filter="active">Active</button>
      <button class="filter-tab" data-filter="done">Done</button>
      <button class="filter-tab" data-filter="error">Failed</button>
    </div>
    <div class="jobs-list" id="jobsList"></div>
    <div class="empty-state" id="emptyJobs">No active download jobs</div>
    <div id="serverFilesList" style="margin-top: 16px;"></div>
  </section>

  <!-- 6. Chat panel (Pollinations AI) -->
  <section class="chat-panel" id="chatPanel" hidden>
    <div class="chat-header">
      <div class="chat-title">
        <svg class="icon"><use href="#i-sparkles"/></svg>
        <span>Musick AI</span>
        <span class="chat-badge">FREE · POLLINATIONS</span>
      </div>
      <button class="icon-btn" id="closeChatBtn" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="chat-body" id="chatBody">
      <div class="chat-empty" id="chatEmpty">Ask me to find songs, recommend playlists, or search for any artist!</div>
      <div class="chat-messages" id="chatMessages"></div>
      <div class="typing-indicator" id="typingIndicator">Musick AI is thinking...</div>
    </div>
    <div class="chat-input-row">
      <input type="text" id="chatInput" placeholder="Ask Musick AI (e.g. Find top Amapiano songs)..." autocomplete="off">
      <button class="btn-primary" id="sendBtn">Send</button>
    </div>
  </section>
</main>

<!-- 7. Bottom nav -->
<nav class="bottom-nav">
  <button class="nav-btn active" data-tab="home">
    <svg class="icon"><use href="#i-home"/></svg>
    <span>Home</span>
  </button>
  <button class="nav-btn" data-tab="downloads">
    <svg class="icon"><use href="#i-download"/></svg>
    <span>Downloads</span>
  </button>
  <button class="nav-btn" data-tab="ai">
    <svg class="icon"><use href="#i-sparkles"/></svg>
    <span>AI</span>
  </button>
</nav>

<!-- 8. Modals -->
<div class="modal-backdrop" id="settingsModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>Settings</h2>
      <button class="icon-btn" id="closeSettingsBtn" data-close="settingsModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="form-section-title">Appearance</div>
      <div class="form-row">
        <label for="setTheme">Theme</label>
        <select id="setTheme" class="select-control">
          <option value="dark">Dark</option>
          <option value="light">Light</option>
        </select>
      </div>
      <div class="form-row">
        <label for="setDensity">Density</label>
        <select id="setDensity" class="select-control">
          <option value="comfortable">Comfortable</option>
          <option value="compact">Compact</option>
        </select>
      </div>
      <div class="form-row">
        <label for="setAccent">Accent Color</label>
        <input type="color" id="setAccent" value="#4d6bfe">
      </div>

      <div class="form-section-title">Defaults</div>
      <div class="form-row">
        <label for="setMode">Default Mode</label>
        <select id="setMode" class="select-control">
          <option value="mp3">Audio (M4A/MP3)</option>
          <option value="mp4">Video (MP4)</option>
        </select>
      </div>
      <div class="form-row">
        <label for="setQuality">Default Quality</label>
        <select id="setQuality" class="select-control">
          <option value="720">720p</option>
          <option value="1080">1080p</option>
          <option value="480">480p</option>
        </select>
      </div>
      <div class="form-row">
        <label for="setBitrate">Default Bitrate</label>
        <select id="setBitrate" class="select-control">
          <option value="128">128 kbps</option>
          <option value="192" selected>192 kbps</option>
          <option value="320">320 kbps</option>
        </select>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn-primary" id="saveSettingsBtn">Save</button>
    </div>
  </div>
</div>

<!-- Preview Modal -->
<div class="modal-backdrop" id="previewModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2 id="previewTitle">Preview</h2>
      <button class="icon-btn" id="closePreviewBtn" data-close="previewModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="preview-player" id="previewPlayer">Loading preview...</div>
      <div class="form-row">
        <label>Mode</label>
        <div class="segmented" id="previewMode">
          <button class="seg-btn active" data-mode="mp3">Audio</button>
          <button class="seg-btn" data-mode="mp4">Video</button>
        </div>
      </div>
      <div class="form-row" id="previewBitrateRow">
        <label for="previewBitrate">Bitrate</label>
        <select id="previewBitrate" class="select-control">
          <option value="128">128 kbps</option>
          <option value="192" selected>192 kbps</option>
          <option value="320">320 kbps</option>
        </select>
      </div>
      <div class="form-row" id="previewQualityRow" hidden>
        <label for="previewQuality">Quality</label>
        <select id="previewQuality" class="select-control">
          <option value="720" selected>720p</option>
          <option value="1080">1080p</option>
          <option value="480">480p</option>
        </select>
      </div>
      <div class="form-row" id="previewFormatRow" hidden>
        <label for="previewFormat">Format</label>
        <select id="previewFormat" class="select-control">
          <option value="">Auto</option>
        </select>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn-primary" id="previewDownloadBtn">Download</button>
    </div>
  </div>
</div>

<!-- History Modal -->
<div class="modal-backdrop" id="historyModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>History</h2>
      <button class="icon-btn" data-close="historyModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <div class="form-section-title">
        <span>Recent Searches</span>
        <button class="btn-text" id="clearSearchesBtn">Clear</button>
      </div>
      <div class="history-list" id="historySearches"></div>
      <div class="form-section-title">
        <span>Download History</span>
        <button class="btn-text" id="clearDownloadsBtn">Clear</button>
      </div>
      <div class="history-list" id="historyDownloads"></div>
    </div>
  </div>
</div>

<!-- Batch Modal -->
<div class="modal-backdrop" id="batchModal" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2>Batch Download</h2>
      <button class="icon-btn" data-close="batchModal" title="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="modal-body">
      <textarea id="batchTextarea" placeholder="One URL per line..."></textarea>
    </div>
    <div class="modal-foot">
      <button class="btn-primary" id="batchQueueBtn">Queue All</button>
    </div>
  </div>
</div>

<!-- Persistent Top Download Progress Bar -->
<div id="dlProgressWrap">
  <div style="display:flex;align-items:center;gap:10px">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a855f7" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
    <div style="flex:1;min-width:0">
      <div id="dlProgressTitle">Downloading...</div>
      <div id="dlProgressSub">Preparing...</div>
    </div>
    <div id="dlProgressPct" style="font-size:13px;font-weight:700;color:#fff">0%</div>
    <button id="dlProgressClose" style="color:#aaa;font-size:16px;padding:2px 6px" title="Dismiss">×</button>
  </div>
  <div id="dlProgressTrack"><div id="dlProgressFill"></div></div>
</div>

<!-- Media Player Bar -->
<div id="trapPlayerBar">
  <video id="trapVideo" playsinline></video>
  <audio id="trapAudio"></audio>
  <div style="display:flex;align-items:center;gap:10px;width:100%">
    <div id="trapThumb" style="width:40px;height:40px;flex-shrink:0;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#a855f7);display:flex;align-items:center;justify-content:center;color:#fff">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
    </div>
    <div style="flex:1;min-width:0">
      <div id="trapTitle" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;font-size:14px;color:#fff">No track</div>
      <div id="trapMode" style="font-size:11px;opacity:.65;color:#fff">Ready</div>
    </div>
    <button id="trapMinBtn" style="width:34px;height:34px;flex-shrink:0;border-radius:50%;background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.12);display:flex;align-items:center;justify-content:center" title="Minimize">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <button id="trapCloseBtn" style="width:34px;height:34px;flex-shrink:0;border-radius:50%;background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.12);display:flex;align-items:center;justify-content:center" title="Close">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div style="width:100%">
    <input id="trapSeek" type="range" min="0" max="100" value="0" style="width:100%;accent-color:#a855f7;display:block">
    <div style="display:flex;justify-content:space-between;width:100%;margin-top:2px">
      <span id="trapCur" style="font-size:11px;opacity:.6;color:#fff">0:00</span>
      <span id="trapDur" style="font-size:11px;opacity:.6;color:#fff">0:00</span>
    </div>
  </div>
  <div style="display:flex;align-items:center;justify-content:center;gap:12px;width:100%">
    <button id="trapSkipBackBtn" style="width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.12);font-size:11px;font-weight:700" title="Back 10s">-10s</button>
    <button id="trapPlayBtn" style="width:52px;height:52px;border-radius:50%;background:#fff;color:#000;border:none;display:flex;align-items:center;justify-content:center" title="Play/Pause">
      <svg id="trapIconPause" width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
      <svg id="trapIconPlay" width="20" height="20" viewBox="0 0 24 24" fill="currentColor" style="display:none"><path d="M8 5v14l11-7z"/></svg>
    </button>
    <button id="trapSkipFwdBtn" style="width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.08);color:#fff;border:1px solid rgba(255,255,255,.12);font-size:11px;font-weight:700" title="Forward 10s">+10s</button>
  </div>
</div>

<!-- 10. Toast Container -->
<div class="toast-container" id="toastContainer"></div>

<script>
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root) => (root || document).querySelectorAll(sel);

  const searchCache = new Map();
  const MAX_CACHE_ENTRIES = 30;
  const MAX_HISTORY_SEARCHES = 12;

  const state = {
    settings: {
      theme: "dark",
      density: "comfortable",
      accent: "#4d6bfe",
      mode: "mp3",
      height: "720",
      abitrate: "192"
    },
    history: {
      searches: [],
      downloads: []
    },
    results: [],
    jobs: [],
    serverFiles: [],
    urlJobMap: {},
    jobFilter: "all",
    activeTab: "home",
    pollTimer: null,
    currentPreview: null,
    chat: {
      messages: [
        {
          role: "system",
          content:
            "You are Musick AI, a helpful and concise music & video discovery assistant. " +
            "Recommend songs, artists, or playlists based on user requests. " +
            "Whenever the user asks to find, play, or download a specific song or genre, include a JSON block at the very end of your response in this exact format: " +
            '```json\n{"action": "search", "query": "Artist - Song Name"}\n```'
        }
      ],
      streaming: false
    }
  };

  /* ---------- Utilities ---------- */
  function haptic(ms = 10) {
    try {
      if (navigator.vibrate) navigator.vibrate(ms);
    } catch (_) {}
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function upgradeThumbUrl(raw, id) {
    const source = String(raw || "");
    // YouTube Music artwork: request a larger square rendition.
    if (source.includes("googleusercontent.com")) {
      return source.replace(/=w\d+-h\d+([^\&\s]*)/, "=w800-h800$1");
    }
    // Standard YouTube video thumbnails: start with 1280x720 when available.
    if (id) return `https://i.ytimg.com/vi/${encodeURIComponent(id)}/maxresdefault.jpg`;
    if (source) {
      return source.replace(/\/(default|mqdefault|sddefault|hqdefault)\.(jpg|webp)/, "/maxresdefault.$2");
    }
    return "";
  }
  function formatDuration(s) {
    if (!s && s !== 0) return "";
    s = Math.floor(Number(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h
      ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
      : `${m}:${String(sec).padStart(2, "0")}`;
  }

  function formatViews(n) {
    if (!n) return "";
    n = Number(n);
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B views`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M views`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K views`;
    return `${n} views`;
  }

  function toast(msg, kind = "") {
    const container = $("toastContainer");
    if (!container) return;
    const el = document.createElement("div");
    el.className = "toast" + (kind ? ` toast-${kind}` : "");
    el.textContent = msg;
    container.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 220);
    }, 2800);
  }
  window.toast = toast;

  async function apiFetch(url, options = {}) {
    const res = await fetch(url, options);
    const contentType = res.headers.get("content-type") || "";
    const data = contentType.includes("application/json")
      ? await res.json()
      : { error: await res.text() };

    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  /* ---------- Theme & Settings ---------- */
  function applyTheme(theme) {
    const t = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    const useEl = $("themeIcon")?.querySelector("use");
    if (useEl) useEl.setAttribute("href", t === "light" ? "#i-moon" : "#i-sun");
  }

  function applyDensity(density) {
    document.documentElement.setAttribute("data-density", density === "compact" ? "compact" : "comfortable");
  }

  function applyAccent(color) {
    if (color) document.documentElement.style.setProperty("--accent", color);
  }

  function setMode(mode) {
    const m = mode === "mp4" ? "mp4" : "mp3";
    $$("#modeToggle .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    $("qualitySelect").hidden = m !== "mp4";
    $("bitrateSelect").hidden = m !== "mp3";
  }

  function getActiveMode() {
    const active = document.querySelector("#modeToggle .seg-btn.active");
    return active ? active.dataset.mode : "mp3";
  }

  function loadSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem("musick_settings") || "{}");
      state.settings = { ...state.settings, ...saved };
    } catch (_) {}

    applyTheme(state.settings.theme);
    applyDensity(state.settings.density);
    applyAccent(state.settings.accent);
    setMode(state.settings.mode);
    if (state.settings.height) $("qualitySelect").value = state.settings.height;
    if (state.settings.abitrate) $("bitrateSelect").value = state.settings.abitrate;
  }

  function saveSettingsToStorage() {
    try {
      localStorage.setItem("musick_settings", JSON.stringify(state.settings));
    } catch (_) {}
  }

  function openSettings() {
    const s = state.settings;
    $("setTheme").value = s.theme || "dark";
    $("setDensity").value = s.density || "comfortable";
    $("setAccent").value = s.accent || "#4d6bfe";
    $("setMode").value = s.mode || "mp3";
    $("setQuality").value = s.height || "720";
    $("setBitrate").value = s.abitrate || "192";
    $("settingsModal").hidden = false;
  }

  function closeSettings() {
    $("settingsModal").hidden = true;
  }

  /* ---------- LocalStorage History & Recent Chips ---------- */
  function loadHistory() {
    try {
      const saved = JSON.parse(localStorage.getItem("musick_history") || "{}");
      state.history = {
        searches: Array.isArray(saved.searches) ? saved.searches : [],
        downloads: Array.isArray(saved.downloads) ? saved.downloads : []
      };
    } catch (_) {}
    renderRecentChips();
  }

  function saveHistory() {
    try {
      localStorage.setItem("musick_history", JSON.stringify(state.history));
    } catch (_) {}
  }

  function addSearchHistory(query) {
    const clean = query.trim();
    if (!clean) return;
    state.history.searches = [
      clean,
      ...state.history.searches.filter((item) => item.toLowerCase() !== clean.toLowerCase())
    ].slice(0, MAX_HISTORY_SEARCHES);
    saveHistory();
    renderRecentChips();
  }

  function addDownloadHistory(entry) {
    state.history.downloads = [entry, ...state.history.downloads].slice(0, 30);
    saveHistory();
  }

  function renderRecentChips() {
    const container = $("recentChipsRow");
    if (!container) return;
    const recent = state.history.searches.slice(0, 6);
    if (!recent.length) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }
    container.hidden = false;
    container.innerHTML =
      `<span class="chips-label">Recent:</span>` +
      recent
        .map((q) => `<button type="button" class="chip recent-chip" data-recent="${escapeHtml(q)}">${escapeHtml(q)}</button>`)
        .join("");

    container.querySelectorAll("[data-recent]").forEach((btn) => {
      btn.addEventListener("click", () => {
        doSearch(btn.dataset.recent);
      });
    });
  }

  /* ---------- Health Check ---------- */
  async function refreshHealth() {
    const pill = $("healthPill");
    const txt = $("healthText");
    try {
      const h = await apiFetch("/api/health");
      const ok = !!(h && h.ok);
      pill.className = "health-pill " + (ok ? "ok" : "bad");
      txt.textContent = ok ? "OK" : "ERR";
    } catch (_) {
      pill.className = "health-pill bad";
      txt.textContent = "OFF";
    }
  }

  /* ---------- Search & In-Memory Caching ---------- */
  async function doSearch(query) {
    const q = (query != null ? query : $("searchInput").value).trim();
    if (!q) return;

    $("searchInput").value = q;
    const btn = $("searchBtn");
    const sp = $("searchProgress");
    const limit = parseInt($("countSelect").value, 10) || 10;
    const cacheKey = `${q.toLowerCase()}::${limit}`;

    addSearchHistory(q);

    if (searchCache.has(cacheKey)) {
      state.results = searchCache.get(cacheKey);
      renderResults();
      return;
    }

    btn.classList.add("loading");
    btn.disabled = true;
    if (sp) sp.hidden = false;

    try {
      const data = await apiFetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, limit })
      });

      if (data.error) {
        throw new Error(data.error);
      }

      const results = (Array.isArray(data.results) ? data.results : []).map((item) => ({
        ...item,
        thumbnail: upgradeThumbUrl(item.thumbnail, item.id)
      }));
      state.results = results;

      if (searchCache.size >= MAX_CACHE_ENTRIES) {
        const oldestKey = searchCache.keys().next().value;
        searchCache.delete(oldestKey);
      }
      searchCache.set(cacheKey, results);

      renderResults();
    } catch (err) {
      toast(`Search failed: ${err.message || "Network error"}`, "error");
    } finally {
      btn.classList.remove("loading");
      btn.disabled = false;
      if (sp) sp.hidden = true;
    }
  }

  function getFilteredResults() {
    const lenFilter = $("lengthFilter").value;
    const sortBy = $("sortSelect").value;
    let list = state.results.slice();

    if (lenFilter !== "all") {
      list = list.filter((r) => {
        const d = Number(r.duration || 0);
        if (lenFilter === "short") return d > 0 && d < 240;
        if (lenFilter === "medium") return d >= 240 && d <= 1200;
        if (lenFilter === "long") return d > 1200;
        return true;
      });
    }

    if (sortBy === "views") {
      list.sort((a, b) => Number(b.view_count || 0) - Number(a.view_count || 0));
    } else if (sortBy === "newest") {
      list.sort((a, b) => Number(b.timestamp || b.upload_date || 0) - Number(a.timestamp || a.upload_date || 0));
    }
    return list;
  }

  function renderResults() {
    const grid = $("resultsGrid");
    const empty = $("resultsEmpty");
    const filtered = getFilteredResults();

    if (!filtered.length) {
      grid.innerHTML = "";
      empty.hidden = false;
      empty.textContent = state.results.length
        ? "No results match your current length filter"
        : "No results found. Try another search.";
      return;
    }
    empty.hidden = true;

    grid.innerHTML = filtered
      .map((r, idx) => {
        const thumb = upgradeThumbUrl(r.thumbnail, r.id);
        const dur = formatDuration(r.duration);
        const views = formatViews(r.view_count);
        const meta = [r.channel, dur, views].filter(Boolean).join(" · ");
        return `
          <div class="result-card" data-idx="${idx}">
            <div class="result-thumb" data-action="preview">
              ${thumb ? `<img src="${escapeHtml(thumb)}" alt="" loading="lazy">` : ""}
              ${dur ? `<span class="duration-badge">${escapeHtml(dur)}</span>` : ""}
            </div>
            <div class="result-title" data-action="preview">${escapeHtml(r.title || "Untitled")}</div>
            <div class="result-meta">${escapeHtml(meta)}</div>
            <div class="result-actions">
              <button class="action-pill" data-action="preview"><svg class="icon"><use href="#i-play"/></svg> Preview</button>
              <button class="action-pill" data-action="mp3"><svg class="icon"><use href="#i-music"/></svg> Audio</button>
              <button class="action-pill" data-action="mp4"><svg class="icon"><use href="#i-video"/></svg> Video</button>
            </div>
            <div class="result-progress"><div class="result-progress-fill"></div></div>
          </div>
        `;
      })
      .join("");

    grid.querySelectorAll(".result-card").forEach((card) => {
      const idx = parseInt(card.dataset.idx, 10);
      const item = filtered[idx];
      if (!item) return;

      // maxresdefault is not published for every video, so fall back cleanly.
      const thumbImg = card.querySelector(".result-thumb img");
      if (thumbImg && item.id) {
        const videoId = encodeURIComponent(item.id);
        const fallbackThumbs = [
          `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`,
          `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg`,
          `https://i.ytimg.com/vi/${videoId}/default.jpg`
        ];
        thumbImg.addEventListener("error", () => {
          const next = Number(thumbImg.dataset.fallbackIndex || 0);
          if (next < fallbackThumbs.length) {
            thumbImg.dataset.fallbackIndex = String(next + 1);
            thumbImg.src = fallbackThumbs[next];
          } else {
            thumbImg.style.visibility = "hidden";
          }
        });
      }

      card.querySelectorAll("[data-action='preview']").forEach((el) => {
        el.addEventListener("click", () => openPreviewByUrl(item.url, item));
      });
      card.querySelector("[data-action='mp3']")?.addEventListener("click", () => {
        queueDownload(item.url, "mp3", { title: item.title });
      });
      card.querySelector("[data-action='mp4']")?.addEventListener("click", () => {
        queueDownload(item.url, "mp4", { title: item.title });
      });
    });

    updateResultProgress();
  }

  /* ---------- Downloads, Jobs & Persistent Progress Polling ---------- */
  function showDownloadProgress(title, pct, statusText) {
    const wrap = $("dlProgressWrap");
    if (!wrap) return;
    const safePct = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    wrap.classList.add("show");
    $("dlProgressTitle").textContent = title || "Downloading...";
    $("dlProgressSub").textContent = statusText || (safePct >= 100 ? "Completed!" : `Downloading... ${safePct}%`);
    $("dlProgressPct").textContent = `${safePct}%`;
    $("dlProgressFill").style.width = `${safePct}%`;

    clearTimeout(window._dlHideTimer);
    if (safePct >= 100) {
      window._dlHideTimer = setTimeout(() => {
        wrap.classList.remove("show");
      }, 3500);
    }
  }

  async function queueDownload(url, mode, extra = {}) {
    if (!url) return null;
    const selectedMode = mode || extra.mode || getActiveMode() || "mp3";
    const title = extra.title || url;

    try {
      showDownloadProgress(title, 2, "Queuing download...");
      const res = await apiFetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          title,
          mode: selectedMode
        })
      });

      if (res.error) throw new Error(res.error);

      if (res.job && res.job.id) {
        state.urlJobMap[url] = res.job.id;
      }

      addDownloadHistory({ title, url, mode: selectedMode, time: Date.now() });
      toast(`Queued: ${title}`, "success");
      startJobPolling();
      return res;
    } catch (err) {
      $("dlProgressWrap")?.classList.remove("show");
      toast(`Download failed: ${err.message}`, "error");
      return null;
    }
  }

  function startJobPolling() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    refreshJobs();
  }

  async function refreshJobs() {
    try {
      const data = await apiFetch("/api/jobs");
      state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
      state.jobs.forEach((j) => {
        if (j.url && j.id) state.urlJobMap[j.url] = j.id;
      });

      renderJobs();
      updateResultProgress();

      const activeJobs = state.jobs.filter((j) =>
        ["queued", "starting", "downloading", "processing"].includes(j.status)
      );

      if (activeJobs.length > 0) {
        const latest = activeJobs[activeJobs.length - 1];
        const statusLabel =
          latest.status === "queued"
            ? "Queued on server..."
            : latest.status === "starting"
            ? "Resolving media stream..."
            : `Downloading${latest.speed ? ` · ${latest.speed}` : ""}`;
        showDownloadProgress(latest.title, latest.progress || 5, statusLabel);

        if (state.pollTimer) clearTimeout(state.pollTimer);
        state.pollTimer = setTimeout(refreshJobs, 1000);
      } else {
        const recentlyDone = state.jobs.find(
          (j) => j.status === "done" && !j._notifiedDone
        );
        if (recentlyDone) {
          recentlyDone._notifiedDone = true;
          showDownloadProgress(recentlyDone.title, 100, "Saved! Ready to play or save.");
          loadServerFiles();
        }
        if (state.pollTimer) {
          clearTimeout(state.pollTimer);
          state.pollTimer = null;
        }
      }
    } catch (_) {
      if (state.pollTimer) {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
      }
    }
  }

  function updateResultProgress() {
    const filtered = getFilteredResults();
    $$("#resultsGrid .result-card").forEach((card) => {
      const idx = parseInt(card.dataset.idx, 10);
      const item = filtered[idx];
      if (!item) return;
      const jobId = state.urlJobMap[item.url];
      const job = jobId ? state.jobs.find((j) => j.id === jobId) : null;
      const bar = card.querySelector(".result-progress");
      const fill = card.querySelector(".result-progress-fill");
      if (!bar || !fill) return;
      if (!job) {
        bar.style.display = "none";
        return;
      }
      bar.style.display = "block";
      fill.className =
        "result-progress-fill" +
        (job.status === "done" ? " done" : job.status === "error" ? " error" : "");
      fill.style.width = `${job.status === "done" ? 100 : Math.max(4, Number(job.progress || 0))}%`;
    });
  }

  function renderJobs() {
    const listEl = $("jobsList");
    const emptyEl = $("emptyJobs");
    const filter = state.jobFilter;

    const filtered = state.jobs
      .slice()
      .sort((a, b) => Number(b.created || 0) - Number(a.created || 0))
      .filter((j) => {
        if (filter === "active") return ["queued", "starting", "downloading", "processing"].includes(j.status);
        if (filter === "done") return j.status === "done";
        if (filter === "error") return j.status === "error" || j.status === "cancelled";
        return true;
      });

    if (!filtered.length) {
      listEl.innerHTML = "";
      emptyEl.hidden = false;
      return;
    }
    emptyEl.hidden = true;

    listEl.innerHTML = filtered
      .map((j) => {
        const pct = j.status === "done" ? 100 : Math.round(Number(j.progress || 0));
        const isActive = ["queued", "starting", "downloading", "processing"].includes(j.status);
        const isFailed = j.status === "error" || j.status === "cancelled";
        const fillCls = j.status === "done" ? " done" : isFailed ? " error" : "";

        let btns = "";
        if (j.status === "done" && j.file) {
          const fileUri = `/api/files/${encodeURIComponent(j.file)}`;
          btns += `<button class="btn-secondary" data-job-action="play" data-uri="${escapeHtml(fileUri)}" data-title="${escapeHtml(j.title || j.file)}" data-mode="${escapeHtml(j.mode || "mp3")}">Play</button>`;
          btns += `<a class="btn-primary" href="${escapeHtml(fileUri)}" download="${escapeHtml(j.file)}" style="text-decoration:none;">Save File</a>`;
        }
        if (isActive) {
          btns += `<button class="btn-secondary" data-job-action="cancel" data-id="${escapeHtml(j.id)}">Cancel</button>`;
        }
        if (isFailed) {
          btns += `<button class="btn-secondary" data-job-action="retry" data-id="${escapeHtml(j.id)}">Retry</button>`;
        }
        btns += `<button class="btn-secondary" data-job-action="remove" data-id="${escapeHtml(j.id)}">Remove</button>`;

        return `
          <div class="job-card">
            <div class="job-head">
              <div class="job-title">${escapeHtml(j.title || j.url || "Download")}</div>
              <span class="job-status status-${escapeHtml(j.status)}">${escapeHtml(j.status)}</span>
            </div>
            <div class="job-meta">
              <span>${escapeHtml((j.mode || "").toUpperCase())}${j.speed ? ` · ${escapeHtml(j.speed)}` : ""}</span>
              <span>${pct}%</span>
            </div>
            <div class="progress-bar"><div class="progress-fill${fillCls}" style="width:${pct}%"></div></div>
            ${j.error ? `<div class="job-error">${escapeHtml(j.error)}</div>` : ""}
            <div class="job-actions">${btns}</div>
          </div>
        `;
      })
      .join("");

    listEl.querySelectorAll("[data-job-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const act = btn.dataset.jobAction;
        try {
          if (act === "play") {
            playMediaFile({
              uri: btn.dataset.uri,
              title: btn.dataset.title,
              type: btn.dataset.mode === "mp4" ? "video" : "audio"
            });
          } else if (act === "cancel") {
            await apiFetch(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
            refreshJobs();
          } else if (act === "retry") {
            await apiFetch(`/api/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" });
            startJobPolling();
          } else if (act === "remove") {
            await apiFetch(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
            refreshJobs();
          }
        } catch (err) {
          toast(`Action failed: ${err.message}`, "error");
        }
      });
    });
  }

  async function loadServerFiles() {
    const box = $("serverFilesList");
    if (!box) return;
    try {
      const data = await apiFetch("/api/files");
      const items = Array.isArray(data.items) ? data.items : [];
      state.serverFiles = items;

      if (!items.length) {
        box.innerHTML = "";
        return;
      }

      box.innerHTML =
        `<div class="form-section-title" style="margin-bottom:8px;"><span>Saved Files (${items.length})</span></div>` +
        items
          .map((it, i) => {
            const isVideo = it.type === "video";
            const cleanTitle = (it.title || "").replace(/\.[^.]+$/, "");
            return `
              <div class="job-card" style="flex-direction:row;align-items:center;gap:12px;padding:12px;margin-bottom:8px;">
                <div style="width:40px;height:40px;flex-shrink:0;border-radius:10px;background:${
                  isVideo ? "linear-gradient(135deg,#a855f7,#7c3aed)" : "linear-gradient(135deg,#3b82f6,#2563eb)"
                };display:flex;align-items:center;justify-content:center;color:#fff;">
                  <svg class="icon"><use href="${isVideo ? "#i-video" : "#i-music"}"/></svg>
                </div>
                <div style="flex:1;min-width:0;">
                  <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;font-size:13.5px;">${escapeHtml(cleanTitle)}</div>
                  <div style="opacity:.65;font-size:11.5px;">${escapeHtml(it.sizeMb || "")} · ${isVideo ? "Video" : "Audio"}</div>
                </div>
                <button class="btn-secondary" data-file-play="${i}" style="padding:7px 12px;">Play</button>
                <a class="btn-primary" href="${escapeHtml(it.uri)}" download="${escapeHtml(it.title)}" style="text-decoration:none;padding:7px 12px;">Save</a>
              </div>
            `;
          })
          .join("");

      box.querySelectorAll("[data-file-play]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const item = state.serverFiles[parseInt(btn.dataset.filePlay, 10)];
          if (item) playMediaFile(item);
        });
      });
    } catch (_) {}
  }

  /* ---------- Preview Modal ---------- */
  async function openPreviewByUrl(url, meta = {}) {
    if (!url) return;
    const modal = $("previewModal");
    modal.hidden = false;
    $("previewTitle").textContent = meta.title || "Preview";
    $("previewPlayer").innerHTML = "Resolving stream...";
    state.currentPreview = { url, title: meta.title || url };

    try {
      const info = await apiFetch("/api/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url })
      });

      if (info.error) throw new Error(info.error);

      const vid = info.video || info;
      const title = vid.title || state.currentPreview.title;
      state.currentPreview.title = title;
      $("previewTitle").textContent = title;

      if (info.stream_url) {
        $("previewPlayer").innerHTML = `<video controls playsinline autoplay src="${escapeHtml(info.stream_url)}"></video>`;
      } else {
        const match = url.match(/(?:v=|\/|youtu\.be\/|shorts\/)([0-9A-Za-z_-]{11})/);
        const ytId = (vid && vid.id) || (match ? match[1] : "");
        if (ytId) {
          $("previewPlayer").innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${escapeHtml(ytId)}?autoplay=1&playsinline=1&rel=0" allow="autoplay; encrypted-media; fullscreen" allowfullscreen></iframe>`;
        } else {
          $("previewPlayer").textContent = "Preview ready to download";
        }
      }

      const fmtSel = $("previewFormat");
      fmtSel.innerHTML = '<option value="">Auto</option>';
      (info.formats || []).forEach((f) => {
        if (!f.format_id) return;
        const opt = document.createElement("option");
        opt.value = f.format_id;
        opt.textContent = [f.format_id, f.ext, f.height ? `${f.height}p` : ""].filter(Boolean).join(" · ");
        fmtSel.appendChild(opt);
      });
    } catch (err) {
      $("previewPlayer").textContent = `Preview error: ${err.message}`;
    }
  }

  function closePreview() {
    const m = $("previewModal");
    if (!m) return;
    m.hidden = true;
    $("previewPlayer").innerHTML = "";
  }

  /* ---------- Free Pollinations AI Chat ---------- */
  function appendChatBubble(role, text) {
    $("chatEmpty").hidden = true;
    const row = document.createElement("div");
    row.className = `msg-row ${role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    row.appendChild(bubble);
    $("chatMessages").appendChild(row);
    $("chatBody").scrollTop = $("chatBody").scrollHeight;
    return bubble;
  }

  async function sendChatMessage(rawText) {
    const text = (rawText != null ? rawText : $("chatInput").value).trim();
    if (!text || state.chat.streaming) return;

    $("chatInput").value = "";
    state.chat.messages.push({ role: "user", content: text });
    appendChatBubble("user", text);

    const botBubble = appendChatBubble("bot", "Thinking...");
    state.chat.streaming = true;
    $("sendBtn").disabled = true;
    $("typingIndicator").classList.add("show");

    try {
      const response = await fetch("https://text.pollinations.ai/openai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "openai",
          messages: state.chat.messages
        })
      });

      if (!response.ok) {
        throw new Error(`Pollinations API returned HTTP ${response.status}`);
      }

      const data = await response.json();
      const replyRaw =
        data?.choices?.[0]?.message?.content ||
        "I found a few ideas for you—what song or artist should we search?";

      const cleanReply = replyRaw.replace(/```json[\s\S]*?```/g, "").trim();
      botBubble.textContent = cleanReply || replyRaw;
      state.chat.messages.push({ role: "assistant", content: replyRaw });

      const jsonMatch =
        replyRaw.match(/```json\s*(\{[\s\S]*?\})\s*```/) ||
        replyRaw.match(/(\{\s*"action"\s*:[\s\S]*?\})/);

      if (jsonMatch) {
        try {
          const cmd = JSON.parse(jsonMatch[1]);
          if (cmd.action === "search" && cmd.query) {
            renderChatInlineSearch(botBubble, cmd.query);
          } else if (cmd.action === "download" && cmd.url) {
            queueDownload(cmd.url, cmd.mode || "mp3", { title: cmd.title || cmd.url });
          }
        } catch (_) {}
      }
    } catch (err) {
      botBubble.textContent = `Sorry, I couldn't reach the AI service: ${err.message}`;
      toast("AI chat error", "error");
    } finally {
      state.chat.streaming = false;
      $("sendBtn").disabled = false;
      $("typingIndicator").classList.remove("show");
      $("chatBody").scrollTop = $("chatBody").scrollHeight;
    }
  }

  async function renderChatInlineSearch(bubble, query) {
    const wrap = document.createElement("div");
    wrap.className = "inline-results";
    const status = document.createElement("div");
    status.className = "inline-searching";
    status.textContent = `Searching for "${query}"...`;
    wrap.appendChild(status);
    bubble.appendChild(wrap);

    try {
      const data = await apiFetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 3 })
      });
      status.remove();

      const items = (Array.isArray(data.results) ? data.results : []).slice(0, 3);
      if (!items.length) {
        const e = document.createElement("div");
        e.className = "inline-empty";
        e.textContent = `No tracks found for "${query}"`;
        wrap.appendChild(e);
        return;
      }

      items.forEach((item) => {
        const thumb = upgradeThumbUrl(item.thumbnail, item.id);
        const card = document.createElement("div");
        card.className = "inline-card";
        card.innerHTML = `
          ${thumb ? `<img src="${escapeHtml(thumb)}" alt="">` : ""}
          <div class="inline-info">
            <div class="inline-title">${escapeHtml(item.title || "")}</div>
            <div class="inline-sub">${escapeHtml(item.channel || "")}</div>
          </div>
          <div class="inline-btns">
            <button class="action-pill" data-dl="mp3" title="Download Audio"><svg class="icon"><use href="#i-music"/></svg></button>
            <button class="action-pill" data-dl="mp4" title="Download Video"><svg class="icon"><use href="#i-video"/></svg></button>
          </div>
        `;
        card.querySelector("[data-dl='mp3']").addEventListener("click", () => {
          queueDownload(item.url, "mp3", { title: item.title });
        });
        card.querySelector("[data-dl='mp4']").addEventListener("click", () => {
          queueDownload(item.url, "mp4", { title: item.title });
        });
        wrap.appendChild(card);
      });
    } catch (err) {
      status.textContent = `Search failed: ${err.message}`;
    }
  }

  /* ---------- Built-In Media Player ---------- */
  function playMediaFile(item) {
    if (!item || !item.uri) {
      toast("No playable file URI", "error");
      return;
    }
    const bar = $("trapPlayerBar");
    const video = $("trapVideo");
    const audio = $("trapAudio");
    const titleEl = $("trapTitle");
    const modeEl = $("trapMode");
    const playIcon = $("trapIconPlay");
    const pauseIcon = $("trapIconPause");

    const isVideo = item.type === "video" || /\.(mp4|webm|mkv)$/i.test(item.uri);
    const title = (item.title || "Playing media").replace(/\.[^.]+$/, "");

    bar.classList.remove("mini", "video-on");
    bar.classList.add("show");
    if (isVideo) bar.classList.add("video-on");
    document.body.classList.add("player-active");

    titleEl.textContent = title;
    modeEl.textContent = isVideo ? "Video · Playing" : "Audio · Playing";

    audio.pause();
    video.pause();
    audio.removeAttribute("src");
    video.removeAttribute("src");

    const activeEl = isVideo ? video : audio;
    activeEl.src = item.uri;
    activeEl.load();
    activeEl.play().then(() => {
      playIcon.style.display = "none";
      pauseIcon.style.display = "block";
    }).catch(() => {
      modeEl.textContent = "Tap play to start";
      playIcon.style.display = "block";
      pauseIcon.style.display = "none";
    });
  }

  function wirePlayerControls() {
    const bar = $("trapPlayerBar");
    const video = $("trapVideo");
    const audio = $("trapAudio");
    const seek = $("trapSeek");
    const curEl = $("trapCur");
    const durEl = $("trapDur");
    const playIcon = $("trapIconPlay");
    const pauseIcon = $("trapIconPause");

    const curMedia = () => (bar.classList.contains("video-on") ? video : audio);
    const fmt = (s) => (!isFinite(s) || s < 0) ? "0:00" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

    $("trapPlayBtn").addEventListener("click", () => {
      const m = curMedia();
      if (m.paused) {
        m.play().catch(() => {});
        playIcon.style.display = "none";
        pauseIcon.style.display = "block";
      } else {
        m.pause();
        playIcon.style.display = "block";
        pauseIcon.style.display = "none";
      }
    });

    $("trapSkipBackBtn").addEventListener("click", () => {
      const m = curMedia();
      m.currentTime = Math.max(0, m.currentTime - 10);
    });

    $("trapSkipFwdBtn").addEventListener("click", () => {
      const m = curMedia();
      m.currentTime = Math.min(m.duration || 999999, m.currentTime + 10);
    });

    $("trapMinBtn").addEventListener("click", () => {
      bar.classList.toggle("mini");
    });

    $("trapCloseBtn").addEventListener("click", () => {
      audio.pause();
      video.pause();
      bar.classList.remove("show", "video-on", "mini");
      document.body.classList.remove("player-active");
    });

    seek.addEventListener("input", () => {
      const m = curMedia();
      if (m.duration) m.currentTime = (seek.value / 100) * m.duration;
    });

    [audio, video].forEach((m) => {
      m.addEventListener("timeupdate", () => {
        if (m !== curMedia()) return;
        curEl.textContent = fmt(m.currentTime);
        durEl.textContent = fmt(m.duration);
        if (m.duration) seek.value = Math.round((m.currentTime / m.duration) * 100);
      });
      m.addEventListener("ended", () => {
        playIcon.style.display = "block";
        pauseIcon.style.display = "none";
      });
    });
  }

  /* ---------- Navigation ---------- */
  function switchTab(tab) {
    const target = tab || "home";
    const isAi = target === "ai";
    document.body.classList.toggle("on-ai-chat", isAi);
    state.activeTab = target;

    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === target));

    $("homeView").hidden = target !== "home";
    $("downloadsSection").hidden = target === "ai";
    $("chatPanel").hidden = !isAi;

    if (target === "downloads") {
      refreshJobs();
      loadServerFiles();
    } else if (isAi) {
      $("chatInput").focus();
    }
  }

  /* ---------- Event Binding ---------- */
  function bindEvents() {
    $("searchBtn").addEventListener("click", () => doSearch($("searchInput").value));
    $("searchInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        doSearch($("searchInput").value);
      }
    });

    $("pasteBtn").addEventListener("click", async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          $("searchInput").value = text.trim();
          toast("Pasted from clipboard");
        }
      } catch (_) {
        toast("Clipboard unavailable", "error");
      }
    });

    $$("#modeToggle .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => setMode(btn.dataset.mode));
    });

    $$("#chipsRow .chip").forEach((chip) => {
      chip.addEventListener("click", () => doSearch(chip.dataset.chip));
    });

    $("lengthFilter").addEventListener("change", renderResults);
    $("sortSelect").addEventListener("change", renderResults);

    $("themeBtn").addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "dark";
      const next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      state.settings.theme = next;
      saveSettingsToStorage();
    });

    $("settingsBtn").addEventListener("click", openSettings);
    $("closeSettingsBtn").addEventListener("click", closeSettings);

    $("saveSettingsBtn").addEventListener("click", () => {
      state.settings = {
        theme: $("setTheme").value,
        density: $("setDensity").value,
        accent: $("setAccent").value,
        mode: $("setMode").value,
        height: $("setQuality").value,
        abitrate: $("setBitrate").value
      };
      saveSettingsToStorage();
      applyTheme(state.settings.theme);
      applyDensity(state.settings.density);
      applyAccent(state.settings.accent);
      setMode(state.settings.mode);
      $("qualitySelect").value = state.settings.height;
      $("bitrateSelect").value = state.settings.abitrate;
      toast("Settings saved", "success");
      closeSettings();
    });

    $("historyBtn").addEventListener("click", () => {
      const searches = state.history.searches || [];
      const downloads = state.history.downloads || [];

      $("historySearches").innerHTML = searches.length
        ? searches
            .map(
              (s) =>
                `<div class="history-item"><span>${escapeHtml(s)}</span><button class="btn-text" data-search-term="${escapeHtml(s)}">Search</button></div>`
            )
            .join("")
        : '<div class="history-item">No searches yet</div>';

      $("historySearches")
        .querySelectorAll("[data-search-term]")
        .forEach((btn) => {
          btn.addEventListener("click", () => {
            $("historyModal").hidden = true;
            switchTab("home");
            doSearch(btn.dataset.searchTerm);
          });
        });

      $("historyDownloads").innerHTML = downloads.length
        ? downloads
            .map(
              (d) =>
                `<div class="history-item"><span>${escapeHtml(d.title || d.url || "")}</span><span>${escapeHtml((d.mode || "").toUpperCase())}</span></div>`
            )
            .join("")
        : '<div class="history-item">No downloads yet</div>';

      $("historyModal").hidden = false;
    });

    $("clearSearchesBtn").addEventListener("click", () => {
      state.history.searches = [];
      saveHistory();
      renderRecentChips();
      $("historySearches").innerHTML = '<div class="history-item">Cleared</div>';
    });

    $("clearDownloadsBtn").addEventListener("click", () => {
      state.history.downloads = [];
      saveHistory();
      $("historyDownloads").innerHTML = '<div class="history-item">Cleared</div>';
    });

    $("batchBtn").addEventListener("click", () => {
      $("batchModal").hidden = false;
    });

    $("batchQueueBtn").addEventListener("click", () => {
      const urls = $("batchTextarea")
        .value.split("\n")
        .map((l) => l.trim())
        .filter(Boolean);
      const mode = getActiveMode();
      urls.forEach((u) => queueDownload(u, mode));
      $("batchTextarea").value = "";
      $("batchModal").hidden = true;
    });

    $$("#previewMode .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = btn.dataset.mode;
        $$("#previewMode .seg-btn").forEach((x) => x.classList.toggle("active", x.dataset.mode === m));
        $("previewBitrateRow").hidden = m !== "mp3";
        $("previewQualityRow").hidden = m !== "mp4";
        $("previewFormatRow").hidden = m !== "mp4";
      });
    });

    $("previewDownloadBtn").addEventListener("click", () => {
      if (!state.currentPreview) return;
      const activeSeg = document.querySelector("#previewMode .seg-btn.active");
      const mode = activeSeg ? activeSeg.dataset.mode : "mp3";
      queueDownload(state.currentPreview.url, mode, {
        abitrate: $("previewBitrate").value,
        height: $("previewQuality").value,
        format_id: $("previewFormat").value,
        title: state.currentPreview.title
      });
      closePreview();
    });

    $("closePreviewBtn").addEventListener("click", closePreview);

    $$("#filterTabs .filter-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.jobFilter = tab.dataset.filter;
        $$("#filterTabs .filter-tab").forEach((t) => t.classList.toggle("active", t === tab));
        renderJobs();
      });
    });

    $("refreshDownloadsBtn").addEventListener("click", () => {
      refreshJobs();
      loadServerFiles();
      toast("Refreshed downloads");
    });

    $("dlProgressClose").addEventListener("click", () => {
      $("dlProgressWrap").classList.remove("show");
    });

    $$(".bottom-nav .nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        haptic(8);
        switchTab(btn.dataset.tab);
      });
    });

    $("closeChatBtn").addEventListener("click", () => switchTab("home"));
    $("sendBtn").addEventListener("click", () => sendChatMessage($("chatInput").value));
    $("chatInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        sendChatMessage($("chatInput").value);
      }
    });

    $$("[data-close]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-close");
        if (id && $(id)) {
          if (id === "previewModal") closePreview();
          else $(id).hidden = true;
        }
      });
    });

    $$(".modal-backdrop").forEach((bd) => {
      bd.addEventListener("click", (e) => {
        if (e.target === bd) {
          if (bd.id === "previewModal") closePreview();
          else bd.hidden = true;
        }
      });
    });
  }

  /* ---------- Init ---------- */
  loadSettings();
  loadHistory();
  bindEvents();
  wirePlayerControls();
  refreshHealth();
  refreshJobs();
  loadServerFiles();
})();
</script>
</body>
</html>'''

# ---------------------------------------------------------------------------
# Serve HTML
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")

@app.get("/<path:p>")
def static_files(p):
    return send_from_directory(BASE_DIR, p)

if __name__ == "__main__":
    print(f"→ http://127.0.0.1:{PORT}")
    print(f"→ downloads: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
