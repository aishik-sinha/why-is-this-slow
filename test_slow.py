import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import slow
from slow import connect, observe, parse_apps, parse_block, parse_counters, parse_mem, rollup, spike_kinds, spikes

def memdb(test):
    db = connect(":memory:")
    test.addCleanup(db.close)
    return db


# Real `top -l` output from this Mac.
BLOCK = """Processes: 518 total, 2 running, 516 sleeping, 3478 threads
2026/11/16 17:21:13
Load Avg: 2.87, 2.77, 3.10
CPU usage: 5.64% user, 8.41% sys, 85.93% idle
PhysMem: 7444M used (1876M wired, 2986M compressor), 178M unused.
VM: 243T vsize, 5430M framework vsize, 46967815(492) swapins, 49415078(0) swapouts.
Networks: packets: 12481130/14G in, 4524318/2265M out.
Disks: 220112334/4353G read, 30359135/1015G written.

PID    COMMAND          %CPU MEM    POWER
398    WindowServer     14.1 1040M- 14.6
49173  Google Chrome He 0.0  23M   0.0
"""


class ParseTest(unittest.TestCase):
    def test_block(self):
        s, procs = parse_block(BLOCK)
        self.assertEqual(s["cpu"], 14.1)
        self.assertEqual(s["load1"], 2.87)
        self.assertEqual(s["mem_free"], 178 * 2**20)
        self.assertEqual(s["swapins"], 46967815)
        self.assertEqual(procs[0], (398, "WindowServer", 14.1, 1040.0, 14.6))
        self.assertEqual(procs[1][1], "Google Chrome He")  # name with spaces survives

    def test_counters(self):
        ioreg = '"Statistics" = {"Bytes (Read)"=4000,"Bytes (Write)"=10}\n"Statistics" = {"Bytes (Read)"=1,"Bytes (Write)"=2}'
        netstat = """Name  Mtu   Network     Address            Ipkts Ierrs  Ibytes Opkts Oerrs Obytes Coll
lo0   16384 <Link#1>                         5     0    999     5     0    999     0
en0   1500  <Link#11>   aa:bb:cc:dd:ee:ff    7     0    300     3     0     40     0
en0   1500  192.168.1   192.168.1.5          7     -    300     3     -     40     -
utun0 1380  <Link#20>                        1     0     50     1     0      5     0
"""
        # loopback skipped, address-less interface counted, per-address duplicate rows ignored
        self.assertEqual(parse_counters(ioreg, netstat),
                         {"disk_read": 4001, "disk_write": 12, "net_in": 350, "net_out": 45})

    def test_spikes_use_deltas(self):
        #      ts load cpu used comp free swapins net_in out read write
        calm = (0, 1, 10, 0, 0, 5e8, 100, 0, 0, 0, 0)
        busy = (5, 1, 95, 0, 0, 1e8, 900, 0, 0, 1e9, 0)
        self.assertEqual(spikes([calm, calm]), [])
        (_, reasons), = spikes([calm, busy])
        self.assertEqual(len(reasons), 3)  # CPU, memory pressure, disk

    def test_spike_kinds(self):
        self.assertEqual(spike_kinds(CALM, CALM), {})
        self.assertEqual(list(spike_kinds(CALM, BUSY)), ["cpu", "mem", "disk"])
        rebooted = (5, 1, 10, 0, 0, 5e8, 0, 0, 0, 0, 0)  # counters restarted below the previous totals
        self.assertEqual(spike_kinds(BUSY, rebooted), {})


#      ts load cpu used comp free swapins net_in out read write
CALM = (0, 1, 10, 0, 0, 5e8, 100, 0, 0, 0, 0)
BUSY = (5, 1, 95, 0, 0, 1e8, 900, 0, 0, 1e9, 0)

# `ps -axo pid=,rss=,comm=`
PS = """  100  2048 /Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper
  101  1024 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
  102  4096 /System/Library/CoreServices/Finder.app/Contents/MacOS/Finder
  103   512 /usr/libexec/logd
  104  1024 /System/Applications/Utilities/Activity Monitor.app/Contents/MacOS/Activity Monitor
"""


def sample(ts, swapins=0, cpu=10):
    return (ts, 1, cpu, 0, 0, 5e8, swapins, 0, 0, 0, 0)


class ObserveTest(unittest.TestCase):
    def test_parse_apps(self):
        # helper collapses into Chrome, CoreServices is not an Applications folder, names keep their spaces
        self.assertEqual(parse_apps(PS), {"Google Chrome", "Activity Monitor"})

    def test_parse_mem(self):
        self.assertEqual(parse_mem(PS), [("Finder", 4.0), ("Google Chrome", 3.0), ("Activity Monitor", 1.0), ("logd", 0.5)])
        self.assertEqual(len(parse_mem(PS, 2)), 2)

    def test_parse_du(self):
        self.assertEqual(slow.parse_du("8\t/r/a b\n20\t/r\n", "/r"), [("/r/a b", 8)])

    def test_observe(self):
        db, state = memdb(self), {}
        count = lambda table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        observe(db, state, None, CALM, [], PS)
        self.assertEqual((count("launch"), count("spike")), (0, 0))  # apps already open are not launches
        self.assertEqual((count("mem"), count("disk_free")), (4, 1))
        observe(db, state, CALM, BUSY, [(1, "hog", 99.0, 1.0, 1.0)], PS + " 105 9 /Applications/Xcode.app/Contents/MacOS/Xcode\n")
        self.assertEqual(db.execute("SELECT * FROM launch").fetchall(), [(5, "Xcode")])
        (ts, kinds, detail, culprit), = db.execute("SELECT * FROM spike")
        self.assertEqual((ts, kinds, culprit), (5, "cpu,mem,disk", "hog"))
        self.assertEqual((count("mem"), count("disk_free")), (4, 1))  # not every sample

    def test_rollup(self):
        db = memdb(self)
        now = 1_000_000 * 3600 + 100
        cur, b, a, old = now - 100, now - 100 - 3600, now - 100 - 7200, now - 100 - 9 * 86400
        db.executemany("INSERT INTO sample VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            sample(old, 5),
            sample(a, 100, cpu=10), sample(a + 5, 150, cpu=20), sample(b - 5, 400, cpu=60),
            sample(b, 10), sample(b + 5, 30),  # rebooted: the counter restarted
            sample(cur + 10, 40)])
        db.executemany("INSERT INTO proc VALUES(?,?,?,?,?,?)", [
            (old, 1, "x", 1, 1, 1), (a, 1, "x", 10, 5, 2.0), (a + 5, 1, "x", 30, 7, 4.0), (a, 2, "y", 1, 1, 0)])
        db.execute("INSERT INTO mem VALUES(?,?,?)", (old, "x", 1))
        db.executemany("INSERT INTO spike VALUES(?,'cpu','CPU 99%','x')", [(old,), (a + 5,)])
        db.execute("INSERT INTO mark VALUES(?,?)", (old, "slow"))
        db.execute("INSERT INTO launch VALUES(?,?)", (old, "Xcode"))
        rollup(db, now)
        hours = db.execute("SELECT hour, n, cpu_avg, cpu_max, swapins, spikes FROM sample_hour ORDER BY hour").fetchall()
        self.assertEqual(hours, [(old, 1, 10, 10, 0, 1), (a, 3, 30, 60, 300, 1), (b, 2, 10, 10, 20, 0)])  # not the current hour
        self.assertEqual(db.execute("SELECT * FROM proc_hour WHERE hour = ? ORDER BY command", (a,)).fetchall(),
                         [(a, "x", 2, 40, 6.0, 7), (a, "y", 1, 1, 0, 1)])
        count = lambda table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        self.assertEqual([count(t) for t in ("sample", "proc", "mem")], [6, 3, 0])  # week-old raw rows dropped
        self.assertEqual([count(t) for t in ("spike", "mark", "launch")], [2, 1, 1])  # kept forever
        before = list(db.iterdump())
        rollup(db, now)
        self.assertEqual(list(db.iterdump()), before)


class SetupTest(unittest.TestCase):
    def test_old_db_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "slow.db", Path(tmp) / "data" / "slow.db"
            db = sqlite3.connect(old)
            db.execute("CREATE TABLE mark(ts REAL, note)")
            db.execute("INSERT INTO mark VALUES(1, 'kept')")
            db.commit()
            db.close()
            with mock.patch.object(slow, "DB", new), mock.patch.object(slow, "OLD_DB", old), \
                    mock.patch.dict(os.environ):
                os.environ.pop("SLOW_DB", None)
                db = connect()
            self.assertEqual(db.execute("SELECT note FROM mark").fetchall(), [("kept",)])
            db.close()
            self.assertTrue(new.exists())
            self.assertFalse(old.exists())

    def test_plist(self):
        p = plistlib.loads(plistlib.dumps(slow.plist()))  # built, never installed
        self.assertEqual(p["Label"], slow.LABEL)
        self.assertEqual(p["ProgramArguments"][1:], [str(slow.APP_DIR / "app" / "slow.py"), "record"])
        self.assertTrue(p["RunAtLoad"] and p["KeepAlive"])
        self.assertEqual(p["Nice"], 10)

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS top")
    def test_stops_when_parent_goes_away(self):
        # the Mac app holds the recorder's stdin; EOF there (app quit or crashed) must stop the recorder
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "slow.db")
            db = connect(path)
            db.execute("INSERT INTO folder VALUES(?,'seed',0)", (time.time(),))  # fresh scan on record: no `du` started
            db.commit()
            db.close()
            p = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("slow.py")), "record"],
                                 env={**os.environ, "SLOW_DB": path, "SLOW_STOP_ON_EOF": "1"},
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(p.kill)
            time.sleep(2)
            self.assertIsNone(p.poll())  # still recording while stdin is open
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
