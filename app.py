import json
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Generator, List, Optional, Set
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, request, render_template_string

app = Flask(__name__)

# ✅ Hardcoded site being searched (change this to your locally hosted video site)
TARGET_BASE_URL = "http://pornhub.com"

DEFAULT_MAX_ATTEMPTS = 300
DEFAULT_TIMEOUT = 20
DEFAULT_SLEEP_S = 0.2
DEFAULT_MATCH_MODE = "word"  # whole-word


# Matches: /view_video.php?viewkey=6961cc79091bd
VIDEO_URL_RE = re.compile(r"/view_video\.php\?viewkey=([^&\"'#\s]+)", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")

# --- Simple in-memory cache for search pages (TTL) ---
CACHE_TTL_S = 30
CACHE_MAX_ITEMS = 200
_page_cache = {}  # url -> (ts, html)


from flask import abort, stream_with_context

@app.get("/thumb")
def thumb_proxy():
    url = (request.args.get("u") or "").strip()
    if not url.startswith("http"):
        abort(400)

    # Fetch it server-side so the browser doesn't hit the remote host directly.
    sess = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        # Some CDNs require a referer to allow thumbnail loads:
        "Referer": TARGET_BASE_URL + "/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    r = sess.get(url, headers=headers, stream=True, timeout=20)
    if r.status_code != 200:
        abort(r.status_code)

    content_type = r.headers.get("Content-Type", "image/jpeg")

    def generate():
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    return Response(stream_with_context(generate()), content_type=content_type)


@dataclass(frozen=True)
class Video:
    url: str
    viewkey: str
    title: Optional[str] = None
    thumb: Optional[str] = None


def normalize_text(s: str) -> str:
    return WHITESPACE_RE.sub(" ", s).strip()


def build_search_url(base_url: str, keyword: str, page: int) -> str:
    # /video/search?search=KEYWORD&page=N
    query = urlencode({"search": keyword, "page": page})
    return urljoin(base_url, f"/video/search?{query}")


def fetch_html(session: requests.Session, url: str, timeout: int = 20) -> str:
    now = time.time()

    # Return cached if fresh
    hit = _page_cache.get(url)
    if hit:
        ts, html = hit
        if now - ts < CACHE_TTL_S:
            return html
        else:
            _page_cache.pop(url, None)

    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    # Insert into cache + trim
    _page_cache[url] = (now, html)
    if len(_page_cache) > CACHE_MAX_ITEMS:
        # evict oldest
        oldest_url = min(_page_cache.items(), key=lambda kv: kv[1][0])[0]
        _page_cache.pop(oldest_url, None)

    return html



def _pick_img_url(img) -> Optional[str]:
    # Prefer "data-mediumthumb" (common for real thumb), then other lazy-load attrs, then src.
    for attr in [
        "data-mediumthumb",
        "data-thumb",
        "data-src",
        "data-original",
        "data-lazy",
        "data-img",
        "src",
    ]:
        val = img.get(attr)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _find_thumbnail_for_anchor(base_url: str, a_tag) -> Optional[str]:
    img = a_tag.find("img")
    if not img:
        parent = a_tag.parent
        for _ in range(4):
            if not parent:
                break
            img = parent.find("img")
            if img:
                break
            parent = parent.parent

    if not img:
        return None

    raw = _pick_img_url(img)
    if not raw:
        return None

    # Make absolute
    return urljoin(base_url, raw)



def extract_videos_from_page(base_url: str, html: str) -> List[Video]:
    soup = BeautifulSoup(html, "html.parser")

    found: List[Video] = []
    seen_keys: Set[str] = set()

    # Matches your exact HTML:
    # <a href="/view_video.php?viewkey=..." title="VIDEO TITLE" class="thumbnailTitle">VIDEO TITLE</a>
    for a in soup.find_all("a", href=True):
        href = a["href"]

        m = VIDEO_URL_RE.search(href)
        if not m:
            continue

        classes = a.get("class") or []
        if "thumbnailTitle" not in classes:
            continue

        viewkey = m.group(1).strip()
        if viewkey in seen_keys:
            continue
        seen_keys.add(viewkey)

        absolute_url = urljoin(base_url, href)

        title_attr = (a.get("title") or "").strip()
        title_text = (a.get_text(" ", strip=True) or "").strip()
        title = normalize_text(title_attr) if title_attr else normalize_text(title_text) if title_text else None

        thumb = _find_thumbnail_for_anchor(base_url, a)
        if thumb:
    # Serve via local proxy to avoid hotlink/CORS issues
            thumb = "/thumb?u=" + requests.utils.quote(thumb, safe="")
        found.append(Video(url=absolute_url, viewkey=viewkey, title=title, thumb=thumb))

    return found


def matches_keywords(video: Video, keywords: List[str], mode: str) -> bool:
    hay = (video.title or "").lower()

    if mode == "substring":
        return any(k.lower() in hay for k in keywords)

    # default: whole word
    return any(re.search(rf"\b{re.escape(k.lower())}\b", hay) for k in keywords)


def sse_event(event: str, data_obj) -> str:
    # SSE format: "event: <name>\ndata: <json>\n\n"
    return f"event: {event}\n" + "data: " + json.dumps(data_obj, ensure_ascii=False) + "\n\n"




def stream_find_videos(
    base_url: str,
    keywords: List[str],
    need: int,
    min_page: int,
    max_page: int,
    max_attempts: int,
    sleep_s: float,
    timeout: int,
    match_mode: str,
) -> Generator[str, None, None]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; LocalVideoFinder/1.0)"
    })

    import math

    # --- dynamic streak limit based on need + keyword count ---
    K = max(1, len(keywords))
    share = math.ceil(need / K)

    PERCENT_OF_SHARE = 0.40
    MIN_BATCH = 1
    MAX_BATCH = 10

    STREAK_LIMIT = math.ceil(share * PERCENT_OF_SHARE)
    STREAK_LIMIT = max(MIN_BATCH, min(MAX_BATCH, STREAK_LIMIT))

    pool_keys: Set[str] = set()
    attempts = 0
    yielded = 0

    # --- quota split across keywords ---
    kw_list = list(keywords)
    k = max(1, len(kw_list))
    base = need // k
    rem = need % k

    quotas = {}
    for i, kw in enumerate(kw_list):
        quotas[kw] = base + (1 if i < rem else 0)

    counts = {kw: 0 for kw in kw_list}

    def remaining_keywords():
        return [kw for kw in kw_list if counts[kw] < quotas[kw]]

    def pick_new_keyword(prev: Optional[str]) -> str:
        candidates = remaining_keywords()
        if not candidates:
            return prev or kw_list[0]
        if prev in candidates and len(candidates) == 1:
            return prev
        if prev in candidates:
            candidates = [c for c in candidates if c != prev] or candidates
        return random.choice(candidates)

    current_keyword = pick_new_keyword(None)
    streak_added = 0

    yield sse_event("meta", {
        "need": need,
        "min_page": min_page,
        "max_page": max_page,
        "quotas": quotas,
        "streak_limit": STREAK_LIMIT,
        "share": share,
        "keywords": len(kw_list),
    })

    while yielded < need and attempts < max_attempts:
        if not remaining_keywords():
            break

        # Switch keyword if streak hit limit OR quota is filled
        if streak_added >= STREAK_LIMIT or counts[current_keyword] >= quotas[current_keyword]:
            current_keyword = pick_new_keyword(current_keyword)
            streak_added = 0

        attempts += 1
        page = random.randint(min_page, max_page)
        search_url = build_search_url(base_url, current_keyword, page)

        try:
            html = fetch_html(session, search_url, timeout=timeout)
        except requests.RequestException as e:
            yield sse_event("status", {
                "attempt": attempts,
                "keyword": current_keyword,
                "page": page,
                "error": str(e),
                "added": 0,
                "total": yielded,
                "streak_added": streak_added,
                "streak_limit": STREAK_LIMIT,
                "kw_count": counts[current_keyword],
                "kw_quota": quotas[current_keyword],
            })
            time.sleep(sleep_s)
            continue

        videos = extract_videos_from_page(base_url, html)
        matches = [v for v in videos if matches_keywords(v, keywords, match_mode)]

        added = 0
        for v in matches:
            if yielded >= need:
                break
            if counts[current_keyword] >= quotas[current_keyword]:
                break
            if streak_added >= STREAK_LIMIT:
                break
            if v.viewkey in pool_keys:
                continue

            pool_keys.add(v.viewkey)
            added += 1
            yielded += 1
            streak_added += 1
            counts[current_keyword] += 1

            payload = asdict(v)
            payload["kw"] = current_keyword
            yield sse_event("video", payload)

        yield sse_event("status", {
            "attempt": attempts,
            "keyword": current_keyword,
            "page": page,
            "found": len(videos),
            "matches": len(matches),
            "added": added,
            "total": yielded,
            "streak_added": streak_added,
            "streak_limit": STREAK_LIMIT,
            "kw_count": counts[current_keyword],
            "kw_quota": quotas[current_keyword],
        })

        time.sleep(sleep_s)

    yield sse_event("done", {
        "attempts": attempts,
        "total": yielded,
        "counts": counts,
        "quotas": quotas
    })




INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>Pornhub Scraper</title>
  <style>
    :root{
      --bg: #0b0b0f;
      --panel: #111118;
      --panel2: #0f0f15;
      --text: #ffffff;
      --muted: #c8c8d4;
      --border: #252533;
      --orange: #ff7a18;
      --orange2: #ff9a3d;
      --shadow: 0 10px 30px rgba(0,0,0,.45);
      --radius: 14px;
    }

    * { box-sizing: border-box; }
    html, body { height: 100%; }

    body{
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      color: var(--text);
      -webkit-text-size-adjust: 100%;

      background:
        radial-gradient(1000px 600px at 10% 0%, rgba(255,122,24,.15), transparent 60%),
        radial-gradient(900px 500px at 90% 10%, rgba(255,154,61,.10), transparent 55%),
        var(--bg);
      background-repeat: no-repeat;
      background-attachment: fixed;
      background-size: cover;
    }

    .wrap { max-width: 1100px; margin: 26px auto; padding: 0 14px; }
    h1 {
        margin: 8px 0 18px;
        font-size: clamp(28px, 6vw, 44px);
        letter-spacing: .4px;
        line-height: 1.05;
    }

    .panel{
      background: linear-gradient(180deg, rgba(255,122,24,.08), transparent 30%), var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px;
      box-shadow: var(--shadow);
    }

    .statsPanel{
    margin-top: 14px;
    padding: 14px;
    }

    .statsTop{
    display:flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 10px;
    }

    .statsTitle{
    font-weight: 1000;
    font-size: 16px;
    letter-spacing: .3px;
    }

    .statsMeta{
    color: var(--muted);
    font-size: 13px;
    }

    .statsGrid{
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    }

    @media (max-width: 860px){
    .statsGrid{ grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 520px){
    .statsGrid{ grid-template-columns: 1fr; }
    }

    .statPill{
    border: 1px solid rgba(255,255,255,.10);
    background: rgba(0,0,0,.22);
    border-radius: 14px;
    padding: 10px 12px;
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap: 10px;
    }

    .statKey{
    color: rgba(255,255,255,.92);
    font-weight: 900;
    font-size: 13px;
    overflow:hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    }

    .statVal{
    color: #111;
    font-weight: 1000;
    font-size: 13px;
    padding: 6px 10px;
    border-radius: 999px;
    background: linear-gradient(180deg, var(--orange2), var(--orange));
    }


    label { display:block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
    input{
      width: 100%;
      font-size: 16px; /* iOS readability */
      padding: 12px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(0,0,0,.25);
      color: var(--text);
      outline: none;
    }
    input:focus{
      border-color: rgba(255,122,24,.75);
      box-shadow: 0 0 0 3px rgba(255,122,24,.18);
    }

    .row { display:grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 14px; }

    .row4{
    grid-template-columns: 2.2fr 0.9fr 0.9fr 0.9fr;
    }

    .bar{
      display:flex; align-items:center; gap: 10px;
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid rgba(255,255,255,.08);
      flex-wrap: wrap;
    }

    button{
      border: 0;
      border-radius: 12px;
      padding: 12px 14px;
      font-weight: 900;
      cursor: pointer;
      transition: transform .06s ease, opacity .15s ease;
      font-size: 15px;
      touch-action: manipulation;
    }
    button:active{ transform: translateY(1px); }
    button[disabled]{ opacity: .5; cursor: not-allowed; }

    #start{
      background: linear-gradient(180deg, var(--orange2), var(--orange));
      color: #111;
    }
    #stop, #spin, #mysteryToggle{
      background: rgba(255,255,255,.08);
      color: var(--text);
      border: 1px solid rgba(255,255,255,.10);
    }

    #status { color: var(--muted); font-size: 13px; flex: 1 1 160px; }

    .cards { margin-top: 14px; display: grid; gap: 12px; padding-bottom: 40px; }

    .card{
      position: relative;
      display:flex; gap: 12px;
      padding: 12px;
      border-radius: var(--radius);
      border: 1px solid rgba(255,255,255,.10);
      background: linear-gradient(180deg, rgba(255,122,24,.06), transparent 40%), var(--panel2);
      transition: transform .12s ease, border-color .12s ease;
    }
    .card:hover{
      transform: translateY(-2px);
      border-color: rgba(255,122,24,.45);
    }

    .thumbWrap{
      position: relative;
      width: 180px;
      height: 102px;
      flex: 0 0 auto;
      border-radius: 12px;
    }
    .thumb{
      width: 100%;
      height: 100%;
      border-radius: 12px;
      object-fit: cover;
      background: rgba(255,255,255,.05);
      border: 1px solid rgba(255,255,255,.08);
      display:block;
    }

    /* Mystery mode (Feature 9) */
    .mystery .thumb{
      filter: blur(10px) saturate(.8);
      transform: scale(1.04);
    }
    .mystery .titleLink{
      color: rgba(255,255,255,.85);
    }

    .thumbOverlay{
      position: absolute;
      inset: 0;
      border-radius: 12px;
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      padding: 10px;
      background: linear-gradient(180deg, transparent 55%, rgba(0,0,0,.60));
      opacity: 0;
      transition: opacity .12s ease;
    }
    .card:hover .thumbOverlay{
      opacity: 1;
    }
    /* On mobile, always show overlay slightly */
    @media (hover: none){
      .thumbOverlay{ opacity: 1; }
    }

    .pill{
      border: 1px solid rgba(255,255,255,.18);
      background: rgba(0,0,0,.35);
      color: #fff;
      border-radius: 999px;
      padding: 8px 11px;
      font-size: 13px;
      font-weight: 900;
      display: inline-flex;
      gap: 8px;
      align-items: center;
      cursor: pointer;
      user-select: none;
    }
    .pill:hover{
      background: rgba(255,122,24,.20);
      border-color: rgba(255,122,24,.55);
    }

    .lockBtn{
      border: 1px solid rgba(255,255,255,.18);
      background: rgba(0,0,0,.35);
      color: #fff;
      border-radius: 999px;
      padding: 8px 11px;
      font-size: 13px;
      font-weight: 900;
      cursor: pointer;
      user-select: none;
    }
    .lockBtn.locked{
      background: rgba(255,122,24,.25);
      border-color: rgba(255,122,24,.65);
    }

    .info { flex: 1; min-width: 0; padding-top: 2px; }
    .titleLink{
      display: inline-block;
      font-weight: 900;
      font-size: 14.5px;
      line-height: 1.25;
      text-decoration: none;
      color: #fff;
    }
    .titleLink:hover{ text-decoration: underline; }

    .hint{
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }

    @media (max-width: 860px){
      .row, .row4 { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px){
      .wrap { margin: 14px auto; padding: 0 12px; }
      .card { flex-direction: column; }
      .thumbWrap { width: 100%; height: auto; aspect-ratio: 16 / 9; }
      .thumb { width: 100%; height: 100%; }
      #status { width: 100%; }
    }

    /* Roulette Overlay (Feature 8) */
    .roulette{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(0,0,0,.65);
      backdrop-filter: blur(6px);
      z-index: 9999;
      padding: 18px;
    }
    .roulette.show{ display: flex; }
    .rouletteCard{
      width: min(520px, 92vw);
      border-radius: 20px;
      border: 1px solid rgba(255,255,255,.12);
      background: linear-gradient(180deg, rgba(255,122,24,.10), transparent 35%), var(--panel);
      box-shadow: 0 20px 60px rgba(0,0,0,.55);
      padding: 16px;
    }
    .rouletteTop{
      display:flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }
    .rouletteTitle{
      font-weight: 1000;
      letter-spacing: .3px;
      margin: 0;
      font-size: 16px;
    }
    .rouletteClose{
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.10);
      color: #fff;
      border-radius: 999px;
      padding: 9px 12px;
      font-weight: 900;
      cursor: pointer;
    }
    .roulettePreview{
      display:flex;
      gap: 12px;
      align-items: center;
    }
    .rouletteImg{
      width: 160px;
      height: 90px;
      border-radius: 14px;
      object-fit: cover;
      border: 1px solid rgba(255,255,255,.10);
      background: rgba(255,255,255,.05);
      flex: 0 0 auto;
    }
    .rouletteName{
      font-weight: 900;
      font-size: 14px;
      line-height: 1.25;
    }
    .rouletteSpin{
      width: 100%;
      margin-top: 14px;
      padding: 14px 14px;
      border-radius: 14px;
      background: linear-gradient(180deg, var(--orange2), var(--orange));
      color: #111;
      font-weight: 1000;
      font-size: 16px;
    }
    .rouletteSub{
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Pornhub Scraper</h1>

    <div class="panel">
      <div class="row row4">
        <div>
            <label>Keywords (comma-separated)</label>
            <input id="keywords" placeholder="Missionary, Goth, Lana Rhoades" />
        </div>
        <div>
            <label>Videos</label>
            <input id="need" type="number" value="20" min="1" />
        </div>
        <div>
            <label>Min Page</label>
            <input id="min_page" type="number" value="1" min="1" />
        </div>
        <div>
            <label>Max Page</label>
            <input id="max_page" type="number" value="50" min="1" />
        </div>
        </div>


      <div class="panel statsPanel" id="statsPanel">
        <div class="statsTop">
            <div class="statsTitle">Stats</div>
            <div class="statsMeta" id="statsSummary">0 videos</div>
        </div>

        <div class="statsGrid" id="statsGrid"></div>
      </div>


      <div class="bar">
        <button id="start">Start</button>
        <button id="stop" disabled>Stop</button>
        <button id="spin" disabled>SPIN</button>
        <button id="mysteryToggle" aria-pressed="false">Mystery: OFF</button>
        <div id="status">Idle. Swipe on the list to spin (after done).</div>
      </div>
    </div>

    <div class="cards" id="cards"></div>
  </div>

  <!-- Roulette Overlay -->
  <div class="roulette" id="roulette">
    <div class="rouletteCard">
      <div class="rouletteTop">
        <h2 class="rouletteTitle">Roulette</h2>
        <button class="rouletteClose" id="rouletteClose">Close</button>
      </div>

      <div class="roulettePreview">
        <img class="rouletteImg" id="rouletteImg" alt="preview" />
        <div class="rouletteName" id="rouletteName">Ready?</div>
      </div>

      <button class="rouletteSpin" id="rouletteSpinBtn">SPIN</button>
      <div class="rouletteSub" id="rouletteSub">Opens a random scraped video.</div>
    </div>
  </div>

<script>
  const LS_KEY_SETTINGS = "local_video_finder_settings_v4";
  const LS_KEY_LOCKS    = "local_video_finder_locked_v1";
  const LS_KEY_MYSTERY  = "local_video_finder_mystery_v1";

  let es = null;
  let collected = [];       // new videos this run
  let lockedMap = {};       // viewkey -> video object
  let mysteryMode = false;  // global toggle
  let revealed = new Set(); // viewkeys revealed (for mystery)
  let runFinished = false;
  let countsByKeyword = {};   // keyword -> count
  let lastKeywordUsed = null; // from status events


  function $(id){ return document.getElementById(id); }

  function vibrate(ms){
    try{ if (navigator.vibrate) navigator.vibrate(ms); }catch{}
  }

  function loadSettings(){
    try{
      const raw = localStorage.getItem(LS_KEY_SETTINGS);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (s.keywords !== undefined) $("keywords").value = s.keywords;
      if (s.need !== undefined) $("need").value = s.need;
      if (s.min_page !== undefined) $("min_page").value = s.min_page;
      if (s.max_page !== undefined) $("max_page").value = s.max_page;
    }catch{}
  }

  function saveSettings(){
    const s = {
      keywords: $("keywords").value,
      need: $("need").value,
      min_page: $("min_page").value,
      max_page: $("max_page").value,
    };
    localStorage.setItem(LS_KEY_SETTINGS, JSON.stringify(s));
  }

  function loadLocks(){
    try{
      const raw = localStorage.getItem(LS_KEY_LOCKS);
      lockedMap = raw ? JSON.parse(raw) : {};
    }catch{
      lockedMap = {};
    }
  }

  function saveLocks(){
    localStorage.setItem(LS_KEY_LOCKS, JSON.stringify(lockedMap));
  }

  function loadMystery(){
    try{
      const raw = localStorage.getItem(LS_KEY_MYSTERY);
      mysteryMode = raw ? JSON.parse(raw) : false;
    }catch{
      mysteryMode = false;
    }
    $("mysteryToggle").setAttribute("aria-pressed", String(mysteryMode));
    $("mysteryToggle").textContent = "Mystery: " + (mysteryMode ? "ON" : "OFF");
  }

  function saveMystery(){
    localStorage.setItem(LS_KEY_MYSTERY, JSON.stringify(mysteryMode));
  }

  ["keywords","need","min_page","max_page"].forEach(id => {
    document.addEventListener("input", (e) => { if (e.target && e.target.id === id) saveSettings(); });
    document.addEventListener("change",(e) => { if (e.target && e.target.id === id) saveSettings(); });
  });

  function setRunning(running){
    $("start").disabled = running;
    $("stop").disabled = !running;
    if (running){
      $("spin").disabled = true;
      runFinished = false;
    }
  }

  function clearNonLocked(){
    // Keep locked videos visible across runs (Feature 4)
    $("cards").innerHTML = "";
    collected = [];
    revealed.clear();
    runFinished = false;
    countsByKeyword = {};
    lastKeywordUsed = null;
    renderStats();


    // Render locked videos first
    const lockedList = Object.values(lockedMap || {});
    for (const v of lockedList){
      addCard(v, true);
    }

    // Spin only available after done
    $("spin").disabled = true;
  }

  function openVideo(url){
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function isLocked(v){ return !!lockedMap[v.viewkey]; }

  function toggleLock(v){
    if (isLocked(v)){
      delete lockedMap[v.viewkey];
      vibrate(30);
    }else{
      lockedMap[v.viewkey] = v;
      vibrate(40);
    }
    saveLocks();
    // Re-render everything (simple & reliable)
    rerenderAll();
  }

  function rerenderAll(){
    $("cards").innerHTML = "";
    // Locked first
    for (const v of Object.values(lockedMap || {})){
      addCard(v, true);
    }
    // Then collected (exclude those that are now locked to avoid duplicate)
    for (const v of collected){
      if (!isLocked(v)) addCard(v, false);
    }
  }

  function displayTitle(v){
    if (!mysteryMode) return v.title || "Untitled";
    if (revealed.has(v.viewkey)) return v.title || "Untitled";
    return "???";
  }

  function applyMysteryCardClasses(card, v){
    if (mysteryMode && !revealed.has(v.viewkey)){
      card.classList.add("mystery");
    }else{
      card.classList.remove("mystery");
    }
  }

  // Long-press helper for mobile (Feature 12)
  function addLongPress(el, onLongPress, ms=450){
    let t = null;
    const start = (e) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => { t=null; onLongPress(e); }, ms);
    };
    const cancel = () => { if (t) clearTimeout(t); t=null; };

    el.addEventListener("touchstart", start, {passive:true});
    el.addEventListener("touchend", cancel);
    el.addEventListener("touchcancel", cancel);
    el.addEventListener("mousedown", start);
    el.addEventListener("mouseup", cancel);
    el.addEventListener("mouseleave", cancel);
  }

  // Card render
  function addCard(v, locked=false){
    const cards = $("cards");

    const div = document.createElement("div");
    div.className = "card";
    applyMysteryCardClasses(div, v);

    const thumbWrap = document.createElement("div");
    thumbWrap.className = "thumbWrap";

    const img = document.createElement("img");
    img.className = "thumb";
    img.alt = v.title || "thumbnail";
    if (v.thumb) img.src = v.thumb;

    const overlay = document.createElement("div");
    overlay.className = "thumbOverlay";

    const playBtn = document.createElement("div");
    playBtn.className = "pill";
    playBtn.textContent = "▶ Play";

    const lockBtn = document.createElement("button");
    lockBtn.type = "button";
    lockBtn.className = "lockBtn" + (locked ? " locked" : "");
    lockBtn.textContent = locked ? "★ Locked" : "☆ Lock";

    overlay.appendChild(playBtn);
    overlay.appendChild(lockBtn);

    thumbWrap.appendChild(img);
    thumbWrap.appendChild(overlay);

    const info = document.createElement("div");
    info.className = "info";

    const titleLink = document.createElement("a");
    titleLink.className = "titleLink";
    titleLink.href = v.url;
    titleLink.target = "_blank";
    titleLink.rel = "noopener noreferrer";
    titleLink.textContent = displayTitle(v);

    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = mysteryMode ? "Tap to reveal • Tap again to open • Long-press to lock" : "Tap to open • Long-press to lock";

    info.appendChild(titleLink);
    info.appendChild(hint);

    // Mystery behavior (Feature 9 + Feature 12)
    const revealOrOpen = () => {
      if (mysteryMode && !revealed.has(v.viewkey)){
        revealed.add(v.viewkey);
        vibrate(20);
        // Update this card in-place
        titleLink.textContent = displayTitle(v);
        applyMysteryCardClasses(div, v);
        return;
      }
      openVideo(v.url);
    };

    // Click targets
    playBtn.addEventListener("click", (e) => { e.stopPropagation(); revealOrOpen(); });
    titleLink.addEventListener("click", (e) => {
      // If mystery and hidden, reveal instead of navigating
      if (mysteryMode && !revealed.has(v.viewkey)){
        e.preventDefault();
        e.stopPropagation();
        revealOrOpen();
      }
    });

    // Whole card tap: reveal/open
    div.addEventListener("click", (e) => {
      // avoid double-trigger if lock button clicked
      if (e.target === lockBtn) return;
      revealOrOpen();
    });

    lockBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleLock(v);
    });

    // Long press anywhere on card to lock/unlock (Feature 12)
    addLongPress(div, (e) => {
      e.preventDefault?.();
      toggleLock(v);
    }, 480);

    div.appendChild(thumbWrap);
    div.appendChild(info);
    cards.appendChild(div); // append keeps locked at top in order rendered
  }

  // Roulette (Feature 8)
  function getAllPlayable(){
    // Prefer unlocked, but allow locked if needed
    const unlocked = collected.filter(v => !isLocked(v));
    const locked = Object.values(lockedMap || {});
    return unlocked.length ? unlocked : locked;
  }

  function showRoulette(){
    if (!runFinished){
      $("status").textContent = "Finish scraping first, then SPIN.";
      vibrate(15);
      return;
    }
    const list = getAllPlayable();
    if (!list.length){
      $("status").textContent = "No videos available.";
      return;
    }
    $("roulette").classList.add("show");
    // Seed preview
    const v = list[Math.floor(Math.random() * list.length)];
    $("rouletteName").textContent = v.title || "Untitled";
    $("rouletteImg").src = v.thumb || "";
    vibrate(10);
  }

  function hideRoulette(){
    $("roulette").classList.remove("show");
  }

  async function spinAndOpen(){
    const list = getAllPlayable();
    if (!list.length) return;

    $("rouletteSpinBtn").disabled = true;

    // spin animation
    const start = performance.now();
    const duration = 2000; // 2s
    let lastPick = null;

    while (performance.now() - start < duration){
      lastPick = list[Math.floor(Math.random() * list.length)];
      $("rouletteName").textContent = lastPick.title || "Untitled";
      $("rouletteImg").src = lastPick.thumb || "";
      vibrate(5);
      // faster then slower
      const t = (performance.now() - start) / duration;
      const delay = 40 + Math.floor(180 * t * t);
      await new Promise(r => setTimeout(r, delay));
    }

    $("rouletteSpinBtn").disabled = false;
    hideRoulette();
    vibrate(30);
    openVideo(lastPick.url);
  }

  // Swipe gesture to open roulette after done (Feature 12)
  function addSwipeToSpin(el){
    let x0=null, y0=null, t0=0;
    el.addEventListener("touchstart", (e) => {
      if (!e.touches || e.touches.length !== 1) return;
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
      t0 = Date.now();
    }, {passive:true});

    el.addEventListener("touchend", (e) => {
      if (x0===null || y0===null) return;
      const dt = Date.now() - t0;
      const touch = (e.changedTouches && e.changedTouches[0]) ? e.changedTouches[0] : null;
      if (!touch) { x0=null; y0=null; return; }
      const dx = touch.clientX - x0;
      const dy = touch.clientY - y0;
      x0=null; y0=null;

      // Horizontal swipe
      if (dt < 600 && Math.abs(dx) > 60 && Math.abs(dy) < 60){
        showRoulette();
      }
    }, {passive:true});
  }

  // Stream controls
  function stopStream(){
    if (es) { es.close(); es = null; }
    setRunning(false);
  }

  function renderStats(){
  const grid = $("statsGrid");
  const total = Object.values(countsByKeyword).reduce((a,b)=>a+b, 0);

  $("statsSummary").textContent = `${total} video${total === 1 ? "" : "s"}`;

  grid.innerHTML = "";

  // Show keywords in descending count
  const entries = Object.entries(countsByKeyword)
    .sort((a,b) => b[1] - a[1]);

  if (!entries.length){
    // Leave empty, but keep the panel space
    return;
  }

  for (const [k, n] of entries){
    const pill = document.createElement("div");
    pill.className = "statPill";

    const key = document.createElement("div");
    key.className = "statKey";
    key.textContent = k;

    const val = document.createElement("div");
    val.className = "statVal";
    val.textContent = n;

    pill.appendChild(key);
    pill.appendChild(val);
    grid.appendChild(pill);
  }
}


  function start(){
    saveSettings();
    clearNonLocked();

    const keywords = $("keywords").value
      .split(",")
      .map(s => s.trim())
      .filter(Boolean)
      .join(",");

    if (!keywords){
      $("status").textContent = "Please enter keywords.";
      return;
    }

    const params = new URLSearchParams({
      keywords: keywords,
      need: $("need").value,
      min_page: $("min_page").value,
      max_page: $("max_page").value,
    });

    setRunning(true);
    $("status").textContent = "Starting…";

    es = new EventSource("/stream?" + params.toString());

    es.addEventListener("meta", (ev) => {
      const m = JSON.parse(ev.data);
      $("status").textContent = `Running… videos=${m.need}, pages=${m.min_page}-${m.max_page}`;
    });

    es.addEventListener("status", (ev) => {
        const s = JSON.parse(ev.data);
        if (s.keyword) lastKeywordUsed = s.keyword;

        if (s.error){
            $("status").textContent = `Attempt ${s.attempt}: error. total=${s.total}`;
        } else {
            $("status").textContent =
            `Attempt ${s.attempt}: kw='${s.keyword}' page=${s.page} added=${s.added} total=${s.total}`;
        }
    });


    es.addEventListener("video", (ev) => {
        const v = JSON.parse(ev.data);

        // Tag the keyword that produced this result (best-effort)
        const kw = v.kw || "unknown";
        countsByKeyword[kw] = (countsByKeyword[kw] || 0) + 1;


        if (!isLocked(v)){
            collected.push(v);
            addCard(v, false);
        }

        renderStats();
    });


    es.addEventListener("done", (ev) => {
      const d = JSON.parse(ev.data);
      runFinished = true;
      $("status").textContent = `Done. Collected ${d.total} videos in ${d.attempts} attempts.`;
      $("spin").disabled = (getAllPlayable().length === 0);
      stopStream();
      vibrate(20);
    });

    es.onerror = () => {
      $("status").textContent = "Stream error / disconnected.";
      $("spin").disabled = true;
      runFinished = false;
      stopStream();
    };
  }

  // Mystery toggle
  function toggleMystery(){
    mysteryMode = !mysteryMode;
    $("mysteryToggle").setAttribute("aria-pressed", String(mysteryMode));
    $("mysteryToggle").textContent = "Mystery: " + (mysteryMode ? "ON" : "OFF");
    saveMystery();
    rerenderAll();
    vibrate(10);
  }

  // Init
  loadSettings();
  loadLocks();
  loadMystery();

  if (!$("keywords").value){
    $("keywords").value = "Missionary, Goth, Lana Rhoades";
    saveSettings();
  }

  // Render locked on load
  clearNonLocked();

  $("start").addEventListener("click", start);
  $("stop").addEventListener("click", () => { runFinished=false; $("spin").disabled=true; stopStream(); });
  $("spin").addEventListener("click", showRoulette);
  $("mysteryToggle").addEventListener("click", toggleMystery);

  $("rouletteClose").addEventListener("click", hideRoulette);
  $("roulette").addEventListener("click", (e) => { if (e.target === $("roulette")) hideRoulette(); });
  $("rouletteSpinBtn").addEventListener("click", spinAndOpen);

  addSwipeToSpin($("cards")); // swipe anywhere on list to open roulette (after done)
</script>
</body>
</html>
"""




@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.get("/stream")
def stream():
    # keywords are space-separated
    kw_str = (request.args.get("keywords") or "").strip()
    keywords = [k.strip() for k in kw_str.split(",") if k.strip()]


    need = int(request.args.get("need", 20))
    min_page = int(request.args.get("min_page", 1))
    max_page = int(request.args.get("max_page", 50))
    max_attempts = int(request.args.get("max_attempts", 300))
    sleep_s = float(request.args.get("sleep_s", 0.4))
    timeout = int(request.args.get("timeout", 20))
    match_mode = (request.args.get("match_mode") or "word").strip()

    if not keywords:
        return Response(sse_event("done", {"attempts": 0, "total": 0}),
                        mimetype="text/event-stream")

    def gen():
        yield from stream_find_videos(
            base_url=TARGET_BASE_URL,
            keywords=keywords,
            need=need,
            min_page=min_page,
            max_page=max_page,
            max_attempts=max_attempts,
            sleep_s=sleep_s,
            timeout=timeout,
            match_mode=match_mode,
        )

    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    # UI server (the finder) runs on localhost:8000
    app.run(host="0.0.0.0", port=8000, debug=True, threaded=True)

