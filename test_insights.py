import unittest

import insights
from slow import connect

def memdb(test):
    db = connect(":memory:")
    test.addCleanup(db.close)
    return db


T1 = 1_000_000 * 3600  # "now": on the hour, so hourly rows line up


def sample(ts, swapins=0, free=1e9):
    return (ts, 1, 10, 0, 0, free, swapins, 0, 0, 0, 0)


class MemoryTest(unittest.TestCase):
    def level(self, swapins, low=0, n=60):
        """n samples (5 min at n=60, so swap-ins/hour = swapins * 12), `low` of them short of free memory."""
        db = memdb(self)
        db.executemany("INSERT INTO sample VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       [sample(T1 - 5 * (n - i), swapins if i else 0, 1e8 if i < low else 1e9) for i in range(n)])
        return insights.memory(db, T1 - 86400, T1)["level"]

    def test_boundaries(self):
        self.assertEqual(self.level(0, n=59), "no_data")
        self.assertEqual(self.level(83), "ok")              # 996/hour
        self.assertEqual(self.level(84), "warn")            # 1,008/hour
        self.assertEqual(self.level(0, low=29), "ok")
        self.assertEqual(self.level(0, low=30), "warn")     # short of memory half the time, but not swapping
        self.assertEqual(self.level(84, low=30), "bad")     # both
        self.assertEqual(self.level(416), "warn")           # 4,992/hour
        self.assertEqual(self.level(417), "bad")            # 5,004/hour

    def test_verdict_names_holders(self):
        db = memdb(self)
        db.executemany("INSERT INTO sample VALUES(?,?,?,?,?,?,?,?,?,?,?)", [sample(T1 - 5 * i) for i in range(60)])
        db.executemany("INSERT INTO mem VALUES(?,?,?)", [(T1 - 60, "Google Chrome", 3174.4), (T1 - 60, "Slack", 512)])
        m = insights.memory(db, T1 - 86400, T1)
        self.assertIn("Biggest holders: Google Chrome 3.1 GB, Slack 0.5 GB", m["verdict"])
        self.assertEqual(m["groups"][0], {"name": "Google Chrome", "rss_mb": 3174.4})


class HogsTest(unittest.TestCase):
    def db(self, hours):
        db = memdb(self)
        for i in range(1, hours + 1):
            h = T1 - 3600 * i
            db.execute("INSERT INTO sample_hour VALUES(?,720,0,0,0,0,0,0,0,0)", (h,))
            db.execute("INSERT INTO proc_hour VALUES(?,'steady',720,0,?,0)", (h, 720 * 3))
            db.execute("INSERT INTO proc_hour VALUES(?,'top',720,0,?,0)", (h, 720 * 50))
        db.execute("INSERT INTO proc_hour VALUES(?,'blip',720,0,?,0)", (T1 - 3600, 720 * 40))
        return db

    def test_blip_ranks_below_steady(self):
        h = insights.hogs(self.db(24), T1 - 86400, T1)
        self.assertEqual([r["command"] for r in h["rows"]], ["steady", "blip"])  # and never `top`, our own probe
        self.assertEqual((h["rows"][0]["score"], h["rows"][0]["presence"]), (3, 1))
        self.assertAlmostEqual(h["rows"][1]["score"], 40 / 24)
        self.assertEqual(h["level"], "warn")
        self.assertIn("steady has used energy in 100% of the hours", h["verdict"])

    def test_needs_a_day(self):
        self.assertEqual(insights.hogs(self.db(23), T1 - 86400, T1)["level"], "no_data")


class LaunchesTest(unittest.TestCase):
    def run_case(self, launches, choked, spike_every_minute=False):
        db = memdb(self)
        db.executemany("INSERT INTO sample_hour VALUES(?,720,0,0,0,0,0,0,0,0)", [(T1 - 3600 * i,) for i in range(1, 11)])
        starts = [T1 - 36000 + 3000 * i + 7 for i in range(launches)]
        db.executemany("INSERT INTO launch VALUES(?,'Xcode')", [(s,) for s in starts])
        db.executemany("INSERT INTO spike VALUES(?,'mem','memory pressure','Xcode')", [(s + 10,) for s in starts[:choked]])
        if spike_every_minute:
            db.executemany("INSERT INTO spike VALUES(?,'cpu','CPU 99%','other')", [(T1 - 36000 + 60 * i,) for i in range(600)])
        return insights.launches(db, T1 - 86400, T1)

    def test_flagged(self):
        out = self.run_case(4, 3)
        row = out["rows"][0]
        self.assertTrue(row["flagged"])
        self.assertEqual((row["launches"], row["choked"], row["rate"], row["kind"], row["culprit"]), (4, 3, 0.75, "mem", "Xcode"))
        self.assertAlmostEqual(row["baseline"], 3 / 600)
        self.assertEqual(out["level"], "warn")
        self.assertIn("When Xcode opens (3 of 4 launches) the Mac hits memory pressure within a minute, 150x", out["verdict"])

    def test_not_flagged_when_spikes_are_constant(self):
        out = self.run_case(4, 3, spike_every_minute=True)
        self.assertEqual((out["rows"][0]["rate"], out["rows"][0]["flagged"], out["level"]), (1, False, "ok"))

    def test_not_flagged_on_two_launches(self):
        self.assertFalse(self.run_case(2, 2)["rows"][0]["flagged"])


class DiskTest(unittest.TestCase):
    def test_days_to_full_and_folder_order(self):
        db = memdb(self)
        db.executemany("INSERT INTO disk_free VALUES(?,?)", [(T1 - 30 * 86400, 500e9), (T1 - 2 * 86400, 100e9), (T1, 60e9)])
        db.executemany("INSERT INTO folder VALUES(?,?,?)", [
            (T1 - 86400, "/x/small", 10), (T1 - 86400, "/x/big", 10), (T1 - 86400, "/x/shrunk", 99),
            (T1, "/x/small", 15), (T1, "/x/big", 60), (T1, "/x/shrunk", 1), (T1, "/x/new", 5)])
        d = insights.disk(db, T1 - 7 * 86400, T1)
        self.assertEqual(d["per_day"], 20e9)  # from the last week, not the month-old reading
        self.assertEqual((d["days_left"], d["level"]), (3, "bad"))
        self.assertEqual([(f["path"], f["growth"]) for f in d["folders"]], [("/x/big", 50 * 1024), ("/x/small", 5 * 1024)])
        self.assertIn("Losing 20.0 GB/day; full in ~3 days. Fastest growing: /x/big", d["verdict"])

    def test_levels(self):
        def level(free_then, free_now):
            db = memdb(self)
            db.executemany("INSERT INTO disk_free VALUES(?,?)", [(T1 - 86400, free_then), (T1, free_now)])
            return insights.disk(db, T1 - 86400, T1)["level"]
        self.assertEqual(level(101e9, 100e9), "ok")    # 100 days left
        self.assertEqual(level(102e9, 100e9), "warn")  # 50 days
        self.assertEqual(level(104e9, 100e9), "bad")   # 25 days
        self.assertEqual(level(5e9, 9e9), "bad")       # growing, but under 10 GB
        self.assertEqual(level(90e9, 100e9), "ok")


class MarksAndSummaryTest(unittest.TestCase):
    def test_marks(self):
        db = memdb(self)
        db.executemany("INSERT INTO mark VALUES(?,?)", [(T1 - 500, "froze"), (T1 - 100, "")])
        db.execute("INSERT INTO spike VALUES(?,'mem','memory pressure (900 swap-ins, 100 MB free)','Xcode')", (T1 - 530,))
        db.execute("INSERT INTO proc VALUES(?,1,'Xcode',80,900,9)", (T1 - 530,))
        m = insights.marks(db, T1 - 3600, T1)
        newest, froze = m["rows"]
        self.assertEqual(froze["verdict"], "memory pressure. Culprit: Xcode.")
        self.assertEqual(froze["procs"][0]["command"], "Xcode")
        self.assertEqual(newest["verdict"], insights.UNEXPLAINED)
        self.assertEqual(m["verdict"], "1 of 2 marks line up with a recorded spike.")

    def test_procs_fall_back_to_hourly(self):
        db = memdb(self)
        db.execute("INSERT INTO sample_hour VALUES(?,720,0,0,0,0,0,0,0,0)", (T1 - 3600,))
        db.execute("INSERT INTO proc_hour VALUES(?,'x',360,7200,720,5)", (T1 - 3600,))
        self.assertEqual(insights.procs_at(db, T1 - 1805, T1 - 1795),
                         ("hour", [{"command": "x", "cpu": 10, "mem_mb": 5, "power": 1}]))

    def test_empty_db(self):
        s = insights.summary(memdb(self), 7)
        self.assertEqual(s["headlines"], [])
        for name in ("memory", "hogs", "launches", "disk", "marks"):
            self.assertEqual((s[name]["level"], s[name]["verdict"]), ("no_data", None))
            self.assertTrue(s[name]["need"])

    def test_headlines_bad_first(self):
        db = memdb(self)
        db.executemany("INSERT INTO disk_free VALUES(?,?)", [(T1 - 86400, 104e9), (T1, 100e9)])  # bad
        db.executemany("INSERT INTO sample VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       [sample(T1 - 5 * (60 - i), 84 if i else 0) for i in range(60)])  # warn
        heads = insights.summary(db, 7, T1)["headlines"]
        self.assertEqual([h.split(":")[0].split(".")[0] for h in heads], ["100", "Memory is tight"])


if __name__ == "__main__":
    unittest.main()
