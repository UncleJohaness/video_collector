#!/usr/bin/env python3
"""
Flask app with mobile-friendly UI + live log streaming via SSE (EventSource).
Run: python app.py
"""

from flask import Flask, render_template, request, jsonify, url_for, send_file, abort
import requests
from bs4 import BeautifulSoup
import random
import time
import re
import urllib.parse
import sys
from urllib.robotparser import RobotFileParser
import threading
import queue
import uuid
import tempfile
import os
from pathlib import Path
import html

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change_me_for_prod")

# ------- Configuration -------
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "https://pornhub.com")
LISTING_URL_FMT = BASE_DOMAIN + "/video?page={}"       # e.g. https://website.com/video?page=1
TOTAL_PAGES = int(os.environ.get("TOTAL_PAGES", "100"))
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; LinkCollector/1.0)")
REQUEST_TIMEOUT = 10                                   # seconds
DELAY_MIN = float(os.environ.get("DELAY_MIN", "0.5"))
DELAY_MAX = float(os.environ.get("DELAY_MAX", "1.0"))
OBEY_ROBOTS = os.environ.get("OBEY_ROBOTS", "True").lower() in ("1","true","yes")
MAX_LINKS_PER_JOB = int(os.environ.get("MAX_LINKS_PER_JOB", "50"))  # safety cap
# -----------------------------

video_href_re = re.compile(r'view_video\.php\?viewkey=', re.IGNORECASE)
background_image_re = re.compile(r'url\((["\']?)(.*?)\1\)', re.IGNORECASE)

# Jobs store: job_id -> {'queue': Queue(), 'finished': bool, 'results': list, 'file': path or None}
jobs = {}
jobs_lock = threading.Lock()

def can_fetch_listing(base_domain, user_agent=USER_AGENT):
    """Return True if robots allows or robots can't be read."""
    try:
        rp = RobotFileParser()
        robots_url = urllib.parse.urljoin(base_domain, "/robots.txt")
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, "/video")
    except Exception:
        return True

def fetch_page_html(url, session):
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException as e:
        return None

def extract_thumb_from_tag(img_tag, base_domain):
    if img_tag is None:
        return None
    for attr in ("src", "data-src", "data-original", "data-lazy", "data-lazy-src"):
        val = img_tag.get(attr)
        if val:
            return urllib.parse.urljoin(base_domain, val)
    srcset = img_tag.get("srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first:
            return urllib.parse.urljoin(base_domain, first)
    return None

def find_thumb_near_link(a_tag, base_domain):
    # 1) descendant img
    img = a_tag.find("img")
    thumb = extract_thumb_from_tag(img, base_domain)
    if thumb:
        return thumb

    # 2) look for sibling or parent img
    parent = a_tag.parent
    if parent:
        img = parent.find("img")
        thumb = extract_thumb_from_tag(img, base_domain)
        if thumb:
            return thumb

    # 3) check inline style background-image on parent or grandparent
    for node in (a_tag, getattr(a_tag, "parent", None), getattr(getattr(a_tag, "parent", None), "parent", None)):
        if node and hasattr(node, "attrs") and node.has_attr("style"):
            m = background_image_re.search(node["style"])
            if m:
                return urllib.parse.urljoin(base_domain, m.group(2))

    # 4) data-thumb attribute or similar on the <a> tag
    for attr in ("data-thumb", "data-thumbnail", "data-image"):
        val = a_tag.get(attr)
        if val:
            return urllib.parse.urljoin(base_domain, val)

    return None

def extract_video_links_from_soup(soup, base_domain):
    found = []
    if soup is None:
        return found
    for a in soup.find_all('a', href=True):
        href = a['href']
        if video_href_re.search(href):
            title = a.get_text(strip=True) or a.get('title', '').strip()
            if not title and a.parent:
                title = a.parent.get_text(" ", strip=True)[:200]
            full = urllib.parse.urljoin(base_domain, href)
            thumb = find_thumb_near_link(a, base_domain)
            found.append({"url": full, "title": title or "", "thumb": thumb})
    return found

def matches_keywords(title, keywords):
    t = (title or "").lower()
    for k in keywords:
        if k and k in t:
            return True
    return False

def find_thumbnail_on_video_page(video_url, session, base_domain):
    soup = fetch_page_html(video_url, session)
    if not soup:
        return None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return urllib.parse.urljoin(base_domain, og["content"])
    linkimg = soup.find("link", rel="image_src")
    if linkimg and linkimg.get("href"):
        return urllib.parse.urljoin(base_domain, linkimg["href"])
    img = soup.find("img")
    if img:
        thumb = extract_thumb_from_tag(img, base_domain)
        if thumb:
            return thumb
    return None

def save_results_html(results, filename=None, title="Collected videos"):
    if filename is None:
        fd, path = tempfile.mkstemp(prefix="collected_", suffix=".html")
        os.close(fd)
    else:
        path = filename
    html_parts = []
    html_parts.append(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;padding:20px;}}
.entry{{margin-bottom:24px;border-bottom:1px solid #ddd;padding-bottom:12px;display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}}
.thumb{{max-width:200px;max-height:120px;object-fit:cover;border-radius:6px;flex:0 0 200px}}
.info{{max-width:760px;flex:1 1 300px}}
.video-title{{font-weight:600;margin:0 0 6px 0;}}
a.link{{word-break:break-all;color:#007acc;text-decoration:none;}}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="list">
""")
    for r in results:
        thumb_html = f'<img class="thumb" src="{html.escape(r.get("thumb",""))}" alt="thumbnail">' if r.get("thumb") else '<div style="width:200px;height:120px;background:#f2f2f2;display:flex;align-items:center;justify-content:center;color:#999;border-radius:6px">No thumbnail</div>'
        safe_title = html.escape(r.get("title") or "")
        html_parts.append(f'''
<div class="entry">
  {thumb_html}
  <div class="info">
    <div class="video-title">{safe_title}</div>
    <div><a class="link" href="{html.escape(r["url"])}">{html.escape(r["url"])}</a></div>
  </div>
</div>
''')
    html_parts.append("""
</div>
</body>
</html>
""")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(html_parts))
    return path

# -------- Background scraping worker --------
def scrape_job(job_id, keywords, desired):
    job = jobs[job_id]
    q = job['queue']
    q.put({"type": "log", "msg": f"Job {job_id}: starting - looking for {desired} link(s) matching {keywords}"})
    if desired > MAX_LINKS_PER_JOB:
        q.put({"type": "log", "msg": f"Desired links ({desired}) exceeds cap ({MAX_LINKS_PER_JOB}). Using cap."})
        desired = MAX_LINKS_PER_JOB

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    if OBEY_ROBOTS:
        q.put({"type": "log", "msg": "Checking robots.txt..."})
        allowed = can_fetch_listing(BASE_DOMAIN)
        if not allowed:
            q.put({"type": "error", "msg": "Robots.txt disallows scraping the listing path. Job aborted."})
            job['finished'] = True
            return

    collected = []
    collected_set = set()
    pages_remaining = list(range(1, TOTAL_PAGES + 1))
    random.shuffle(pages_remaining)

    q.put({"type": "log", "msg": f"Scanning up to {TOTAL_PAGES} pages randomly..."})

    while pages_remaining and len(collected) < desired:
        page = pages_remaining.pop()
        page_url = LISTING_URL_FMT.format(page)
        q.put({"type": "log", "msg": f"Checking page {page}..."})
        soup = fetch_page_html(page_url, session)
        if soup is None:
            q.put({"type": "log", "msg": f"  - failed to fetch page {page}, skipping."})
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            continue

        videos = extract_video_links_from_soup(soup, BASE_DOMAIN)
        matched_this_page = 0
        for v in videos:
            if v["url"] in collected_set:
                continue
            if matches_keywords(v.get("title", ""), keywords):
                if not v.get("thumb"):
                    v["thumb"] = find_thumbnail_on_video_page(v["url"], session, BASE_DOMAIN)
                collected.append(v)
                collected_set.add(v["url"])
                matched_this_page += 1
                q.put({"type": "match", "msg": f"Matched: {v.get('title')!r}", "item": v})
                if len(collected) >= desired:
                    break

        if matched_this_page == 0:
            q.put({"type": "log", "msg": "  - No matches on this page."})

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    job['results'] = collected
    if collected:
        q.put({"type": "log", "msg": f"Collected {len(collected)} link(s). Preparing result file..."})
        path = save_results_html(collected)
        job['file'] = path
        q.put({"type": "done", "msg": "Job finished.", "file": os.path.basename(path)})
    else:
        q.put({"type": "done", "msg": "Job finished. No links found.", "file": None})

    job['finished'] = True

# -------- Routes --------
@app.route("/")
def index():
    return render_template("index.html", base_domain=BASE_DOMAIN, obey_robots=OBEY_ROBOTS, max_links=MAX_LINKS_PER_JOB)

@app.route("/collect", methods=["POST"])
def collect():
    raw = request.form.get("keywords", "")
    desired = request.form.get("desired", "10")
    try:
        desired = int(desired)
        if desired <= 0:
            return jsonify({"error": "Enter a positive integer for number of links."}), 400
    except ValueError:
        return jsonify({"error": "Invalid number."}), 400

    keywords = [k.strip().lower() for k in raw.split(",") if k.strip()]
    if not keywords:
        return jsonify({"error": "Enter at least one keyword."}), 400

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    with jobs_lock:
        jobs[job_id] = {"queue": q, "finished": False, "results": [], "file": None}

    # Start background thread
    t = threading.Thread(target=scrape_job, args=(job_id, keywords, desired), daemon=True)
    t.start()

    return jsonify({"job_id": job_id}), 202

@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in jobs:
        return abort(404)

    def event_stream():
    q = jobs[job_id]['queue']
    last_ping = time.time()  # track last heartbeat

    while True:
        try:
            obj = q.get(timeout=0.5)
        except queue.Empty:
            # <-- this block MUST be indented relative to except
            if time.time() - last_ping > 10:
                yield "data: {}\n\n"  # heartbeat
                last_ping = time.time()
            
            if jobs[job_id]['finished'] and q.empty():
                break
            continue

        import json
        payload = json.dumps(obj, default=str)
        yield f"data: {payload}\n\n"

    # final message
    yield f"data: {json.dumps({'type':'final','msg':'stream closed'})}\n\n"

    return app.response_class(event_stream(), mimetype="text/event-stream")

@app.route("/results/<job_id>")
def results(job_id):
    if job_id not in jobs:
        return abort(404)
    return jsonify({"results": jobs[job_id]['results'], "file": os.path.basename(jobs[job_id]['file']) if jobs[job_id]['file'] else None, "finished": jobs[job_id]['finished']})

@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs:
        return abort(404)
    path = jobs[job_id].get('file')
    if not path or not os.path.exists(path):
        return abort(404)
    return send_file(path, as_attachment=True, download_name="results.html", mimetype="text/html")

if __name__ == "__main__":
    # debug True is fine for local testing; set to False in production
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
