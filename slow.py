#!/usr/bin/env python3
"""Why is this slow? Records what your Mac is doing and explains spikes afterwards.

  python3 slow.py record          # leave running; samples every 5s into slow.db
  python3 slow.py mark "laggy"    # run the moment it feels slow
  python3 slow.py why [minutes]   # explain the last N minutes (default 30)
"""
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB = Path(__file__).with_name("slow.db")
INTERVAL = 5
TOP_N = 10  # processes kept per sample
UNITS = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

# A spike is any sample past one of these lines.
CPU_SPIKE = 80        # % of all cores busy
SWAPIN_SPIKE = 500    # pages swapped in during one sample: memory pressure, the classic lag cause
DISK_SPIKE = 100e6    # bytes/s
NET_SPIKE = 20e6      # bytes/s


def size(s):
    """'1040M-' -> bytes."""
    m = re.match(r"([\d.]+)([BKMGT])", s)
    return float(m[1]) * UNITS[m[2]] if m else 0.0


def parse_block(text):
    """Parse one sample of `top -l` output into (system dict, [process tuples])."""
    sysd, procs, in_table = {}, [], False
    for line in text.splitlines():
        if re.match(r"\d{4}/\d\d/\d\d \d\d:\d\d:\d\d", line):
            # top's own clock: piped output is buffered, so arrival time would be late
            sysd["ts"] = time.mktime(time.strptime(line.strip(), "%Y/%m/%d %H:%M:%S"))
        elif line.startswith("Load Avg:"):
            sysd["load1"] = float(line.split()[2].rstrip(","))
        elif line.startswith("CPU usage:"):
            sysd["cpu"] = round(100 - float(re.search(r"([\d.]+)% idle", line)[1]), 1)
        elif line.startswith("PhysMem:"):
            m = re.search(r"(\S+) used .*?(\S+) compressor\), (\S+) unused", line)
            sysd["mem_used"], sysd["compressor"], sysd["mem_free"] = map(size, m.groups())
        elif line.startswith("VM:"):
            sysd["swapins"] = int(re.search(r"(\d+)\(\d+\) swapins", line)[1])
        elif line.startswith("PID"):
            in_table = True
        elif in_table and line.strip():
            parts = line.split()
            # COMMAND can contain spaces, so peel fixed columns off both ends.
            pid, cpu, mem, power = parts[0], parts[-3], parts[-2], parts[-1]
            procs.append((int(pid), " ".join(parts[1:-3]), float(cpu), size(mem) / UNITS["M"], float(power)))
    return sysd, procs


def parse_counters(ioreg, netstat):
    """Exact running byte totals. top's own Disks/Networks lines are rounded to whole GB."""
    disk = {k: sum(map(int, re.findall(rf'"Bytes \({k}\)"=(\d+)', ioreg))) for k in ("Read", "Write")}
    net_in = net_out = 0
    for line in netstat.splitlines():
        p = line.split()
        if "<Link#" in line and not p[0].startswith("lo"):
            # parse from the right: the Address column is blank for some interfaces
            net_in, net_out = net_in + int(p[-5]), net_out + int(p[-2])
    return {"disk_read": disk["Read"], "disk_write": disk["Write"], "net_in": net_in, "net_out": net_out}


def counters():
    run = lambda *c: subprocess.run(c, capture_output=True, text=True).stdout
    return parse_counters(run("ioreg", "-c", "IOBlockStorageDriver", "-r", "-w0", "-k", "Statistics"),
                          run("netstat", "-ibn"))


def connect():
    db = sqlite3.connect(DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sample(ts REAL PRIMARY KEY, load1, cpu, mem_used, compressor, mem_free,
            swapins, net_in, net_out, disk_read, disk_write);
        CREATE TABLE IF NOT EXISTS proc(ts REAL, pid, command, cpu, mem_mb, power);
        CREATE INDEX IF NOT EXISTS proc_ts ON proc(ts);
        CREATE TABLE IF NOT EXISTS mark(ts REAL, note);
    """)
    return db


def record():
    db = connect()
    cmd = ["top", "-l", "0", "-s", str(INTERVAL), "-stats", "pid,command,cpu,mem,power", "-o", "cpu", "-n", str(TOP_N)]
    top = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, errors="replace")
    print(f"Recording every {INTERVAL}s into {DB}. Ctrl+C to stop.")
    block, first = [], True
    try:
        for line in top.stdout:
            if line.startswith("Processes:") and block:
                if not first:  # top's first sample has no CPU deltas yet
                    sysd, procs = parse_block("".join(block))
                    store(db, sysd | counters(), procs)
                first, block = False, []
            block.append(line)
    except KeyboardInterrupt:
        pass
    finally:
        top.terminate()


COLS = ("ts", "load1", "cpu", "mem_used", "compressor", "mem_free", "swapins",
        "net_in", "net_out", "disk_read", "disk_write")


def store(db, sysd, procs):
    if any(k not in sysd for k in COLS):
        return  # partial block (top was interrupted); skip rather than store garbage
    ts = sysd["ts"]
    db.execute("INSERT OR IGNORE INTO sample VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(sysd[k] for k in COLS))
    db.executemany("INSERT INTO proc VALUES(?,?,?,?,?,?)", [(ts, *p) for p in procs])
    db.commit()


def mb(b):
    return f"{b / 1e6:,.0f} MB"


def hhmm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def culprits(db, t0, t1, n=3):
    rows = db.execute("""SELECT command, AVG(cpu), MAX(mem_mb) FROM proc WHERE ts BETWEEN ? AND ?
                         GROUP BY command ORDER BY AVG(cpu) DESC LIMIT ?""", (t0, t1, n)).fetchall()
    return ", ".join(f"{c} ({cpu:.0f}% CPU, {m:,.0f} MB)" for c, cpu, m in rows) or "no process data"


def spikes(rows):
    """rows: sample rows in time order -> [(ts, [reasons])]. Uses deltas of the running totals."""
    out = []
    for prev, cur in zip(rows, rows[1:]):
        dt = (cur[0] - prev[0]) or 1
        reasons = []
        if cur[2] >= CPU_SPIKE:
            reasons.append(f"CPU {cur[2]:.0f}%")
        if cur[6] - prev[6] >= SWAPIN_SPIKE:
            reasons.append(f"memory pressure ({cur[6] - prev[6]:,} swap-ins, {mb(cur[5])} free)")
        if (d := (cur[9] - prev[9] + cur[10] - prev[10]) / dt) >= DISK_SPIKE:
            reasons.append(f"disk {mb(d)}/s")
        if (n := (cur[7] - prev[7] + cur[8] - prev[8]) / dt) >= NET_SPIKE:
            reasons.append(f"network {mb(n)}/s")
        if reasons:
            out.append((cur[0], reasons))
    return out


def why(minutes=30):
    db = connect()
    t1 = time.time()
    t0 = t1 - minutes * 60
    rows = db.execute("SELECT * FROM sample WHERE ts >= ? ORDER BY ts", (t0,)).fetchall()
    if len(rows) < 2:
        sys.exit(f"Not enough data in the last {minutes} min. Run `python3 slow.py record` first.")
    a, z = rows[0], rows[-1]
    print(f"== Last {minutes} min ({len(rows)} samples, {hhmm(a[0])} to {hhmm(z[0])}) ==")
    print(f"CPU avg {sum(r[2] for r in rows) / len(rows):.0f}%, max {max(r[2] for r in rows):.0f}%")
    print(f"Free memory min {mb(min(r[5] for r in rows))}, compressed max {mb(max(r[4] for r in rows))}, "
          f"swap-ins {z[6] - a[6]:,}")
    print(f"Disk read {mb(z[9] - a[9])}, written {mb(z[10] - a[10])}; network in {mb(z[7] - a[7])}, out {mb(z[8] - a[8])}")

    print("\nBiggest CPU users:", culprits(db, t0, t1, 5))
    hogs = db.execute("""SELECT command, AVG(power) FROM proc WHERE ts >= ? GROUP BY command
                         ORDER BY AVG(power) DESC LIMIT 5""", (t0,)).fetchall()
    print("Biggest battery drains (energy impact):", ", ".join(f"{c} ({p:.1f})" for c, p in hogs))

    found = spikes(rows)
    print(f"\n== Spikes ({len(found)}) ==" if found else "\nNo spikes. Things looked calm.")
    last = 0
    for ts, reasons in found:
        if ts - last < 60:  # one line per burst, not per sample
            continue
        last = ts
        print(f"{hhmm(ts)}  {'; '.join(reasons)}\n          during: {culprits(db, ts - INTERVAL, ts)}")

    for ts, note in db.execute("SELECT * FROM mark WHERE ts >= ?", (t0,)):
        print(f"\n== You marked {hhmm(ts)}: {note or 'slow'} ==")
        print(f"Running in the minute before: {culprits(db, ts - 60, ts + INTERVAL, 5)}")
        near = [s for s in found if ts - 60 <= s[0] <= ts + INTERVAL]
        print("Spikes in that minute: " + ("; ".join(r for _, rs in near for r in rs) if near else
              "none. The cause may be outside what this records (GPU, heat throttling, one app hanging)."))


if __name__ == "__main__":
    cmd, args = (sys.argv[1:2] or ["why"])[0], sys.argv[2:]
    if cmd == "record":
        record()
    elif cmd == "mark":
        db = connect()
        db.execute("INSERT INTO mark VALUES(?,?)", (time.time(), " ".join(args)))
        db.commit()
        print("Marked. Run `python3 slow.py why` to see what was going on.")
    elif cmd == "why":
        why(int(args[0]) if args else 30)
    else:
        sys.exit(__doc__)
