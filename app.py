from flask import Flask, request, render_template, send_file, redirect, url_for, flash
import requests
from bs4 import BeautifulSoup
import random
import time
import re
import urllib.parse
import os
import tempfile
from urllib.robotparser import RobotFileParser

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change_me")

# ---------- Config (change as needed or use env vars) ----------
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "https://pornhub.com")
LISTING_URL_FMT = BASE_DOMAIN + "/video?page={}"
TOTAL_PAGES = int(os.environ.get("TOTAL_PAGES", "100"))
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; LinkCollector/1.0)")
REQUEST_TIMEOUT = 10
DELAY_MIN = 0.8
DELAY_MAX = 1.6
OBEY_ROBOTS = False
# ---------------------------------------------------------------

video_href_re = re.compile(r'view_video\.php\?viewkey=', re.IGNORECASE)
background_image_re = re.compile(r'url\((["\']?)(.*?)\1\)', re.IGNORECASE)

def can_fetch_listing(base_domain, user_agent=USER_AGENT):
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
    except requests.RequestException:
        return None

def extract_thumb_from_tag(img_tag, base_domain):
    if not img_tag:
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
    img = a_tag.find("img")
    thumb = extract_thumb_from_tag(img, base_domain)
    if thumb:
        return thumb
    parent = a_tag.parent
    if parent:
        img = parent.find("img")
        thumb = extract_thumb_from_tag(img, base_domain)
        if thumb:
            return thumb
    for node in (a_tag, a_tag.parent, getattr(a_tag.parent, "parent", None)):
        if node and node.has_attr("style"):
            m = background_image_re.search(node["style"])
            if m:
                return urllib.parse.urljoin(base_domain, m.group(2))
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
            title = a.get_text(strip=True) or a.get('title','').strip()
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

def collect_videos(keywords, desired):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if OBEY_ROBOTS:
        allowed = can_fetch_listing(BASE_DOMAIN)
        if not allowed:
            return {"error": "robots_disallowed", "message": "Robots.txt disallows scraping the listing path."}

    collected = []
    collected_set = set()
    pages_remaining = list(range(1, TOTAL_PAGES+1))
    random.shuffle(pages_remaining)
    while pages_remaining and len(collected) < desired:
        page = pages_remaining.pop()
        page_url = LISTING_URL_FMT.format(page)
        soup = fetch_page_html(page_url, session)
        if soup is None:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            continue
        videos = extract_video_links_from_soup(soup, BASE_DOMAIN)
        for v in videos:
            if v["url"] in collected_set:
                continue
            if matches_keywords(v.get("title",""), keywords):
                if not v.get("thumb"):
                    v["thumb"] = find_thumbnail_on_video_page(v["url"], session, BASE_DOMAIN)
                collected.append(v)
                collected_set.add(v["url"])
                if len(collected) >= desired:
                    break
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    return {"error": None, "results": collected}

def save_results_html(results):
    fd, path = tempfile.mkstemp(prefix="collected_", suffix=".html")
    os.close(fd)
    html_parts = []
    html_parts.append("""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Collected videos</title>
    <style>body{font-family:Arial,Helvetica,sans-serif;padding:20px} .entry{margin-bottom:18px;border-bottom:1px solid #ddd;padding-bottom:12px;display:flex;gap:12px;align-items:flex-start}.thumb{max-width:200px;max-height:120px;object-fit:cover;border-radius:6px}.info{max-width:760px}.video-title{font-weight:600;margin:0 0 6px 0} a.link{word-break:break-all;color:#007acc;text-decoration:none}</style>
    </head><body><h1>Collected videos</h1><div class="list">""")
    for r in results:
        thumb_html = f'<img class="thumb" src="{r["thumb"]}" alt="thumbnail">' if r.get("thumb") else '<div style="width:200px;height:120px;background:#f2f2f2;display:flex;align-items:center;justify-content:center;color:#999;border-radius:6px">No thumbnail</div>'
        safe_title = (r.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
        html_parts.append(f'<div class="entry">{thumb_html}<div class="info"><div class="video-title">{safe_title}</div><div><a class="link" href="{r["url"]}">{r["url"]}</a></div></div></div>')
    html_parts.append("</div></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(html_parts))
    return path

# ---------- Routes ----------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", base_domain=BASE_DOMAIN, obey_robots=OBEY_ROBOTS)

@app.route("/collect", methods=["POST"])
def collect():
    raw = request.form.get("keywords", "")
    desired = request.form.get("desired", "10")
    try:
        desired = int(desired)
        if desired <= 0:
            raise ValueError
    except ValueError:
        flash("Enter a positive integer for number of links.")
        return redirect(url_for("index"))

    keywords = [k.strip().lower() for k in raw.split(",") if k.strip()]
    if not keywords:
        flash("Enter at least one keyword.")
        return redirect(url_for("index"))

    result = collect_videos(keywords, desired)
    if result["error"] == "robots_disallowed":
        flash("Robots.txt disallows scraping the listing path. To proceed, set OBEY_ROBOTS=False in environment.")
        return redirect(url_for("index"))
    results = result["results"]
    # Save HTML and return page with inline results + download link
    out_path = save_results_html(results)
    # store path in session-free way by returning link with filename
    download_name = os.path.basename(out_path)
    # move to /tmp with known name so send_file can access it by name
    target = os.path.join(tempfile.gettempdir(), download_name)
    os.replace(out_path, target)
    return render_template("results.html", results=results, download_file=download_name, count=len(results))

@app.route("/download/<filename>", methods=["GET"])
def download(filename):
    path = os.path.join(tempfile.gettempdir(), filename)
    if not os.path.exists(path):
        flash("File not found or expired.")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True, download_name="results.html", mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
