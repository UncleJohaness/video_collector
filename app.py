from flask import Flask, render_template, request, jsonify, Response, abort
import threading, queue, time, random
import requests
from bs4 import BeautifulSoup
import json

app = Flask(__name__)

# ----------------------
# Job storage
# ----------------------
jobs = {}  # job_id -> {'queue': Queue(), 'finished': bool}

# ----------------------
# Scraper function
# ----------------------
def scrape_videos(job_id, keywords, num_links):
    q = jobs[job_id]['queue']
    collected_links = []
    pages_cleared = set()
    DELAY_MIN, DELAY_MAX = 0.5, 1.0  # reduce delay for faster log updates

    while len(collected_links) < num_links:
        page_num = random.randint(1, 100)
        if page_num in pages_cleared:
            continue
        pages_cleared.add(page_num)

        q.put({'type':'log', 'msg': f"Fetching page {page_num}..."})
        try:
            r = requests.get(f"https://pornhub.com/video?page={page_num}", timeout=10)
            r.raise_for_status()
        except Exception as e:
            q.put({'type':'error', 'msg': f"Failed to fetch page {page_num}: {e}"})
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        videos = soup.find_all("a", class_="video-link")  # adjust selector
        found = False
        for vid in videos:
            title = vid.get("title", "")
            href = vid.get("href", "")
            thumb = vid.find("img")["src"] if vid.find("img") else ""
            if any(k.lower() in title.lower() for k in keywords):
                collected_links.append({'title': title, 'link': href, 'thumbnail': thumb})
                q.put({'type':'video', 'title': title, 'link': href, 'thumbnail': thumb})
                found = True
                if len(collected_links) >= num_links:
                    break
        if not found:
            q.put({'type':'log', 'msg': f"No matching videos on page {page_num}"})

        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # mark job finished
    jobs[job_id]['finished'] = True
    q.put({'type':'final', 'msg':'Scraping finished!'})

# ----------------------
# Routes
# ----------------------
@app.route("/")
def index():
    return render_template("index.html")  # your mobile-friendly template

@app.route("/collect", methods=["POST"])
def collect():
    data = request.json
    keywords = data.get("keywords", [])
    num_links = int(data.get("num_links", 5))

    if not keywords or num_links <= 0:
        return jsonify({'error':'Invalid input'}), 400

    # create a new job
    job_id = str(time.time())  # simple unique ID
    q = queue.Queue()
    jobs[job_id] = {'queue': q, 'finished': False}

    # start background thread
    t = threading.Thread(target=scrape_videos, args=(job_id, keywords, num_links))
    t.start()

    return jsonify({'job_id': job_id})

@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in jobs:
        return abort(404)

    def event_stream():
        q = jobs[job_id]['queue']
        last_ping = time.time()
        while True:
            try:
                obj = q.get(timeout=0.5)
            except queue.Empty:
                # send heartbeat every 10s
                if time.time() - last_ping > 10:
                    yield "data: {}\n\n"
                    last_ping = time.time()
                if jobs[job_id]['finished'] and q.empty():
                    break
                continue
            yield f"data: {json.dumps(obj)}\n\n"

        # final message
        yield f"data: {json.dumps({'type':'final','msg':'stream closed'})}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")

# ----------------------
# Run
# ----------------------
if __name__ == "__main__":
    app.run(debug=True, threaded=True)