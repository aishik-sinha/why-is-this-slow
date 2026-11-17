import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from server import make_server
from slow import connect

JSON = {"Content-Type": "application/json"}


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "slow.db")
        db, now = connect(cls.db_path), time.time()
        db.executemany("INSERT INTO sample VALUES(?,1,?,0,0,5e8,?,0,0,0,?)",
                       [(now - 10, 10, 100, 0), (now - 5, 50, 90, 5e6)])  # swap-in counter went backwards: a reboot
        db.execute("INSERT INTO proc VALUES(?,1,'<img src=x onerror=alert(1)>',50,100,3)", (now - 5,))
        db.execute("INSERT INTO spike VALUES(?,'cpu,mem','CPU 99%','x')", (now - 5,))
        db.commit()
        db.close()
        cls.server = make_server(cls.db_path, 0)  # port 0: any free port
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers={}):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body, headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, r.getheader("Content-Type"), data, r

    def get_json(self, path):
        status, ctype, data, _ = self.request("GET", path)
        self.assertEqual((status, ctype), (200, "application/json"), data)
        return json.loads(data)

    def test_index(self):
        status, ctype, data, r = self.request("GET", "/")
        self.assertEqual((status, ctype), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<html", data)
        self.assertNotIn(b"innerHTML", data)  # process names are attacker-chosen: the page must never parse them
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_live(self):
        live = self.get_json("/api/live")
        self.assertEqual(live["procs"][0]["command"], "<img src=x onerror=alert(1)>")
        self.assertLess(live["last_sample_age"], 30)
        (p,) = live["samples"]
        self.assertEqual((p["cpu"], p["swapins_s"], p["disk_bps"]), (50, 0, 1e6))  # reset clamps to 0, never negative

    def test_timeline_and_at(self):
        t = self.get_json("/api/timeline")
        self.assertEqual((t["resolution"], len(t["points"])), ("raw", 1))
        self.assertEqual(t["spikes"][0]["kinds"], ["cpu", "mem"])
        self.assertEqual(self.get_json("/api/timeline?from=0&to=1000")["resolution"], "hour")
        at = self.get_json(f"/api/at?ts={t['points'][0]['ts']}")
        self.assertEqual((at["resolution"], at["procs"][0]["cpu"], len(at["spikes"])), ("raw", 50, 1))

    def test_insights(self):
        s = self.get_json("/api/insights?days=30")
        self.assertEqual((s["days"], s["memory"]["level"]), (30, "no_data"))

    def test_bad_requests(self):
        for path in ("/api/timeline?from=abc", "/api/timeline?from=nan", "/api/timeline?from=5&to=1", "/api/at",
                     "/api/at?ts=-1", "/api/insights?days=0", "/api/insights?days=1.5"):
            status, _, data, _ = self.request("GET", path)
            self.assertEqual(status, 400, path)
            self.assertIn("error", json.loads(data))
        for path in ("/nope", "/slow.py", "/web/index.html", "/../slow.py"):  # nothing but / is served from disk
            self.assertEqual(self.request("GET", path)[0], 404, path)
        self.assertEqual(self.request("OPTIONS", "/api/mark")[0], 501)  # no CORS preflight answered

    def test_dns_rebinding_blocked(self):
        self.assertEqual(self.request("GET", "/api/live", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/mark", '{"note":"x"}', {**JSON, "Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/live", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_mark(self):
        db = connect(self.db_path)
        self.addCleanup(db.close)
        marks = lambda: db.execute("SELECT ts, note FROM mark ORDER BY ts").fetchall()
        note = '{"note": "froze"}'
        self.assertEqual(self.request("POST", "/api/mark", "note=froze", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/mark", note, {**JSON, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/mark", "{not json", JSON)[0], 400)
        self.assertEqual(self.request("POST", "/api/mark", '{"note": 5}', JSON)[0], 400)
        self.assertEqual(self.request("POST", "/api/mark", '["note"]', JSON)[0], 400)
        self.assertEqual(self.request("POST", "/api/mark", "[" * 4000, JSON)[0], 400)  # deeper than the JSON parser goes
        self.assertEqual(self.request("POST", "/api/mark", '{"note": "\\ud800"}', JSON)[0], 400)  # cannot be stored as UTF-8
        self.assertEqual(self.request("POST", "/api/mark", json.dumps({"note": "x" * 5000}), JSON)[0], 413)
        self.assertEqual(self.request("POST", "/api/other", note, JSON)[0], 404)
        self.assertEqual(marks(), [])
        status, _, data, _ = self.request("POST", "/api/mark", note, {**JSON, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(marks(), [(json.loads(data)["ts"], "froze")])
        self.request("POST", "/api/mark", json.dumps({"note": "y" * 300}), JSON)
        self.assertEqual(marks()[-1][1], "y" * 200)
        self.assertEqual(self.get_json("/api/live")["marks_today"], 2)


if __name__ == "__main__":
    unittest.main()
