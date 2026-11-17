"""Local dashboard for slow.py: one HTML page and a JSON API, on 127.0.0.1 only.

No login: anything running as you can already read the database. What this must stop is a web page
in your browser reading your process list, hence the Host and Origin checks and no CORS headers.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import insights
from slow import INTERVAL, PORT, RAW_DAYS, connect, deltas

INDEX = Path(__file__).with_name("web") / "index.html"
MAX_POINTS = 1000
MAX_BODY = 4096
MAX_NOTE = 200


def num(q, key, default, lo, hi, cast=float):
    try:
        v = cast(q.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not lo <= v <= hi:  # also rejects nan
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return v


def points(rows, width):
    """Sample rows -> chart points, one per `width` seconds: avg cpu, min free memory, max compressed, rates."""
    buckets = {}
    for prev, cur in zip(rows, rows[1:]):
        buckets.setdefault(cur[0] // width, []).append((cur, deltas(prev, cur)))
    out = []
    for group in buckets.values():
        rs, ds = [g[0] for g in group], [g[1] for g in group]
        dt, swapins = sum(d[0] for d in ds), sum(d[1] for d in ds)
        out.append({"ts": rs[0][0], "cpu": sum(r[2] for r in rs) / len(rs), "load1": sum(r[1] for r in rs) / len(rs),
                    "mem_free": min(r[5] for r in rs), "compressor": max(r[4] for r in rs), "swapins": swapins,
                    "swapins_s": swapins / dt, "disk_bps": sum(d[2] for d in ds) / dt, "net_bps": sum(d[3] for d in ds) / dt})
    return out


def dicts(cur):
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur]


def live(db, q, now):
    rows = db.execute("SELECT * FROM sample WHERE ts >= ? ORDER BY ts", (now - 600,)).fetchall()
    last = db.execute("SELECT MAX(ts) FROM sample").fetchone()[0]
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    return {"now": now, "last_sample_age": None if last is None else now - last, "samples": points(rows, INTERVAL / 2),
            "procs": dicts(db.execute("""SELECT command, cpu, mem_mb, power FROM proc
                                         WHERE ts = (SELECT MAX(ts) FROM proc) ORDER BY cpu DESC""")),
            "mem": dicts(db.execute("SELECT name, rss_mb FROM mem WHERE ts = (SELECT MAX(ts) FROM mem) ORDER BY rss_mb DESC")),
            "marks_today": db.execute("SELECT COUNT(*) FROM mark WHERE ts >= ?", (midnight,)).fetchone()[0]}


def timeline(db, q, now):
    t0, t1 = num(q, "from", now - 3600, 0, 1e11), num(q, "to", now, 0, 1e11)
    if t0 >= t1:
        raise ValueError("from must be before to")
    # an hour of slack: raw rows are only dropped on the hour, and the page's clock is not ours
    if t0 >= now - RAW_DAYS * 86400 - 3600 and t1 - t0 <= RAW_DAYS * 86400:
        resolution = "raw"
        rows = db.execute("SELECT * FROM sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1)).fetchall()
        pts = points(rows, max(INTERVAL / 2, (t1 - t0) / MAX_POINTS))
    else:
        resolution = "hour"
        pts = dicts(db.execute("""SELECT hour AS ts, cpu_avg AS cpu, mem_free_min AS mem_free, compressor_max AS compressor,
            swapins, swapins * 1.0 / (n * ?1) AS swapins_s, disk_bytes * 1.0 / (n * ?1) AS disk_bps,
            net_bytes * 1.0 / (n * ?1) AS net_bps FROM sample_hour WHERE hour BETWEEN ?2 AND ?3 ORDER BY hour""",
                               (INTERVAL, t0, t1)))
    newest = lambda sql, cap: dicts(db.execute(f"SELECT * FROM ({sql} WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT ?) "
                                               "ORDER BY ts", (t0, t1, cap)))
    spikes = newest("SELECT ts, kinds, detail, culprit FROM spike", 2000)
    for s in spikes:
        s["kinds"] = s["kinds"].split(",")
    return {"resolution": resolution, "points": pts, "spikes": spikes,
            "marks": newest("SELECT ts, note FROM mark", 5000), "launches": newest("SELECT ts, app FROM launch", 5000)}


def at(db, q, now):
    if "ts" not in q:
        raise ValueError("ts is required")
    ts = num(q, "ts", None, 0, 1e11)
    resolution, procs = insights.procs_at(db, ts - INTERVAL, ts + INTERVAL)
    spikes = dicts(db.execute("SELECT ts, kinds, detail, culprit FROM spike WHERE ts BETWEEN ? AND ? ORDER BY ts",
                              (ts - 30, ts + 30)))
    for s in spikes:
        s["kinds"] = s["kinds"].split(",")
    return {"ts": ts, "resolution": resolution, "procs": procs, "spikes": spikes,
            "mem": dicts(db.execute("""SELECT name, rss_mb FROM mem WHERE ts = (SELECT MAX(ts) FROM mem WHERE ts BETWEEN ? AND ?)
                                       ORDER BY rss_mb DESC""", (ts - 90, ts + INTERVAL)))}


def summary(db, q, now):
    return insights.summary(db, num(q, "days", 7, 1, 3650, int), now)


ROUTES = {"/api/live": live, "/api/timeline": timeline, "/api/at": at, "/api/insights": summary}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # the dashboard polls every 5s; don't fill the launchd log

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        # the page talks to this server, plus Google Fonts for the pixel typeface (no data is sent there)
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                         "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        """True if the request names this server by a local name. A rebinding attack arrives as evil.example."""
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        url = urlsplit(self.path)
        if not self.local():
            return self.reply(403, {"error": "bad Host header"})
        if url.path == "/":
            return self.reply(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if url.path not in ROUTES:
            return self.reply(404, {"error": "not found"})
        db = connect(self.server.db_path)
        try:
            self.reply(200, ROUTES[url.path](db, dict(parse_qsl(url.query)), time.time()))
        except ValueError as e:
            self.reply(400, {"error": str(e)})
        finally:
            db.close()

    def do_POST(self):
        port, h = self.server.server_port, self.headers
        if not self.local():
            return self.reply(403, {"error": "bad Host header"})
        if urlsplit(self.path).path != "/api/mark":
            return self.reply(404, {"error": "not found"})
        # a cross-site form cannot send application/json without a CORS preflight, which we never answer
        if h.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.reply(415, {"error": "Content-Type must be application/json"})
        if h.get("Origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self.reply(403, {"error": "bad Origin"})
        try:
            length = int(h.get("Content-Length", ""))
            if not 0 <= length <= MAX_BODY:
                return self.reply(413, {"error": "body too large"})
            note = json.loads(self.rfile.read(length))["note"]
            if not isinstance(note, str):
                raise ValueError
            note.encode()  # a lone surrogate (\ud800) is valid JSON but cannot be stored
        except (ValueError, KeyError, TypeError, RecursionError):
            return self.reply(400, {"error": 'body must be JSON like {"note": "text"}'})
        ts = time.time()
        db = connect(self.server.db_path)
        db.execute("INSERT INTO mark VALUES(?,?)", (ts, note[:MAX_NOTE]))
        db.commit()
        db.close()
        self.reply(200, {"ts": ts})


def make_server(db_path, port=PORT):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db_path = db_path
    return server
