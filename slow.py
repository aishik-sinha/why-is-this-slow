#!/usr/bin/env python3
"""Why is this slow? Records what your Mac is doing and explains spikes afterwards.

  python3 slow.py install         # record in the background from login on (launchd), dashboard at 127.0.0.1:8765
  python3 slow.py uninstall       # stop the background recorder; data is kept
  python3 slow.py record          # record in this terminal instead; also serves the dashboard
  python3 slow.py mark "laggy"    # run the moment it feels slow
  python3 slow.py why [minutes]   # explain the last N minutes (default 30)
"""
import fcntl
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# Not next to the script: launchd processes may not read ~/Desktop, ~/Documents or ~/Downloads.
APP_DIR = Path.home() / "Library/Application Support/why-is-this-slow"
DB = APP_DIR / "slow.db"  # override with env SLOW_DB
OLD_DB = Path(__file__).with_name("slow.db")
LABEL = "com.github.aishik-sinha.why-is-this-slow"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
PORT = 8765
INTERVAL = 5
RAW_DAYS = 7           # per-sample rows kept this long; hourly rollups are kept forever
MEM_EVERY = 12         # samples between memory-by-app snapshots (60s)
DISK_EVERY = 120       # samples between free-disk readings (10 min)
FOLDER_EVERY_S = 21600
# Only caches and app data. Never Desktop, Documents, Downloads, Containers or Group Containers.
FOLDER_ROOTS = [Path.home() / "Library" / d for d in ("Caches", "Application Support", "Developer", "Logs")]
APP_ROOTS = ("/Applications/", "/System/Applications/", str(Path.home() / "Applications") + "/",
             "/System/Volumes/Preboot/Cryptexes/App/System/Applications/")  # last one: where Safari lives
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


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace").stdout


def counters():
    return parse_counters(run("ioreg", "-c", "IOBlockStorageDriver", "-r", "-w0", "-k", "Statistics"),
                          run("netstat", "-ibn"))


def app_name(comm):
    """Executable path -> app bundle name. Helpers collapse into their parent app; None if not an app."""
    if comm.startswith(APP_ROOTS):
        for part in comm.split("/"):
            if part.endswith(".app"):
                return part[:-4]


def ps_rows(ps_text):
    """`ps -axo pid=,rss=,comm=` -> (rss kB, executable path) per process."""
    for line in ps_text.splitlines():
        p = line.split(None, 2)
        if len(p) == 3 and p[1].isdigit():
            yield int(p[1]), p[2]


def parse_apps(ps_text):
    return {a for _, comm in ps_rows(ps_text) if (a := app_name(comm))}


def parse_mem(ps_text, n=10):
    """Memory summed per app (or per command name for non-apps) -> top n [(name, MB)]."""
    groups = {}
    for rss, comm in ps_rows(ps_text):
        name = app_name(comm) or comm.rsplit("/", 1)[-1]
        groups[name] = groups.get(name, 0) + rss / 1024
    return sorted(groups.items(), key=lambda g: -g[1])[:n]


def parse_du(text, root):
    """`du -k -d 1` output -> [(child path, kB)], without the root's own total."""
    rows = (line.split("\t", 1) for line in text.splitlines() if "\t" in line)
    return [(path, int(kb)) for kb, path in rows if path != str(root)]


def connect(path=None):
    path = path or os.environ.get("SLOW_DB") or DB
    if str(path) == str(DB):
        DB.parent.mkdir(parents=True, exist_ok=True)
        if OLD_DB.exists() and not DB.exists():
            shutil.move(OLD_DB, DB)  # history from before the database left the script's folder
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA journal_mode=WAL")  # the dashboard reads while the recorder writes
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sample(ts REAL PRIMARY KEY, load1, cpu, mem_used, compressor, mem_free,
            swapins, net_in, net_out, disk_read, disk_write);
        CREATE TABLE IF NOT EXISTS proc(ts REAL, pid, command, cpu, mem_mb, power);
        CREATE INDEX IF NOT EXISTS proc_ts ON proc(ts);
        CREATE TABLE IF NOT EXISTS mark(ts REAL, note);
        CREATE TABLE IF NOT EXISTS spike(ts REAL, kinds TEXT, detail TEXT, culprit TEXT);
        CREATE INDEX IF NOT EXISTS spike_ts ON spike(ts);
        CREATE TABLE IF NOT EXISTS launch(ts REAL, app TEXT);
        CREATE TABLE IF NOT EXISTS mem(ts REAL, name TEXT, rss_mb REAL);
        CREATE INDEX IF NOT EXISTS mem_ts ON mem(ts);
        CREATE TABLE IF NOT EXISTS disk_free(ts REAL, free REAL);
        CREATE TABLE IF NOT EXISTS folder(ts REAL, path TEXT, kb INTEGER);
        CREATE TABLE IF NOT EXISTS sample_hour(hour REAL PRIMARY KEY, n, cpu_avg, cpu_max, mem_free_min,
            compressor_max, swapins, disk_bytes, net_bytes, spikes);
        CREATE TABLE IF NOT EXISTS proc_hour(hour REAL, command TEXT, rows, cpu_sum, power_sum, mem_max,
            PRIMARY KEY(hour, command));
    """)
    return db


def record():
    path = str(os.environ.get("SLOW_DB") or DB)
    db = connect(path)
    lock = open(Path(path).with_name("record.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one writer per database
    except OSError:
        sys.exit("already recording (launchd agent running? see slow.py uninstall)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # launchd stops us with SIGTERM; still run the finally
    try:
        from server import make_server
        threading.Thread(target=make_server(path, PORT).serve_forever, daemon=True).start()
        print(f"Dashboard: http://127.0.0.1:{PORT}", flush=True)
    except OSError as e:
        print(f"warning: no dashboard, port {PORT} is taken ({e}). Still recording.", flush=True)
    threading.Thread(target=scan_folders, args=(path,), daemon=True).start()
    cmd = ["top", "-l", "0", "-s", str(INTERVAL), "-stats", "pid,command,cpu,mem,power", "-o", "cpu", "-n", str(TOP_N)]
    top = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, errors="replace")
    print(f"Recording every {INTERVAL}s into {path}. Ctrl+C to stop.", flush=True)
    block, first, state, prev = [], True, {}, None
    try:
        for line in top.stdout:
            if line.startswith("Processes:") and block:
                if not first:  # top's first sample has no CPU deltas yet
                    sysd, procs = parse_block("".join(block))
                    if cur := store(db, sysd | counters(), procs):
                        observe(db, state, prev, cur, procs, run("ps", "-axo", "pid=,rss=,comm="))
                        prev = cur
                first, block = False, []
            block.append(line)
    except KeyboardInterrupt:
        pass
    finally:
        top.terminate()


COLS = ("ts", "load1", "cpu", "mem_used", "compressor", "mem_free", "swapins",
        "net_in", "net_out", "disk_read", "disk_write")


def store(db, sysd, procs):
    """Save one sample; returns its row, or None for a partial block."""
    if any(k not in sysd for k in COLS):
        return  # partial block (top was interrupted); skip rather than store garbage
    row = tuple(sysd[k] for k in COLS)
    db.execute("INSERT OR IGNORE INTO sample VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    db.executemany("INSERT INTO proc VALUES(?,?,?,?,?,?)", [(row[0], *p) for p in procs])
    db.commit()
    return row


def observe(db, state, prev, cur, procs, ps_text):
    """Everything derived from a new sample: spikes, app launches, memory by app, free disk, hourly rollup.
    state is a dict that lives as long as the recorder; prev is the previous sample row (None at start)."""
    ts = cur[0]
    if prev and (kinds := spike_kinds(prev, cur)):
        db.execute("INSERT INTO spike VALUES(?,?,?,?)",
                   (ts, ",".join(kinds), "; ".join(kinds.values()), procs[0][1] if procs else ""))
    apps = parse_apps(ps_text)
    if "apps" in state:  # everything already open when recording starts is not a launch
        db.executemany("INSERT INTO launch VALUES(?,?)", [(ts, a) for a in sorted(apps - state["apps"])])
    state["apps"] = apps
    n = state["n"] = state.get("n", -1) + 1
    if n % MEM_EVERY == 0:
        db.executemany("INSERT INTO mem VALUES(?,?,?)", [(ts, name, m) for name, m in parse_mem(ps_text)])
    if n % DISK_EVERY == 0:
        db.execute("INSERT INTO disk_free VALUES(?,?)", (ts, shutil.disk_usage("/System/Volumes/Data").free))
    db.commit()
    if state.get("hour") != ts // 3600:
        state["hour"] = ts // 3600
        rollup(db, ts)


def rollup(db, now):
    """Fold each complete hour not yet in sample_hour into hourly rows, then drop raw rows past RAW_DAYS.
    Safe to repeat; on first run it back-fills all recorded history."""
    last = db.execute("SELECT MAX(hour) FROM sample_hour").fetchone()[0]
    span = (0 if last is None else last + 3600, now // 3600 * 3600)
    for h, in db.execute("SELECT DISTINCT CAST(ts / 3600 AS INT) * 3600 FROM sample WHERE ts >= ? AND ts < ?", span).fetchall():
        # the minute before the hour supplies the row the first delta is measured from (not across a sleep gap)
        rows = db.execute("SELECT * FROM sample WHERE ts >= ? AND ts < ? ORDER BY ts", (h - 60, h + 3600)).fetchall()
        own = [r for r in rows if r[0] >= h]
        seq = rows[-len(own) - 1:]
        d = [deltas(a, b) for a, b in zip(seq, seq[1:])]
        db.execute("""INSERT OR REPLACE INTO sample_hour VALUES(?,?,?,?,?,?,?,?,?,
                      (SELECT COUNT(*) FROM spike WHERE ts >= ? AND ts < ?))""",
                   (h, len(own), sum(r[2] for r in own) / len(own), max(r[2] for r in own), min(r[5] for r in own),
                    max(r[4] for r in own), sum(x[1] for x in d), sum(x[2] for x in d), sum(x[3] for x in d),
                    h, h + 3600))
        db.execute("""INSERT OR REPLACE INTO proc_hour SELECT ?, command, COUNT(*), SUM(cpu), SUM(power), MAX(mem_mb)
                      FROM proc WHERE ts >= ? AND ts < ? GROUP BY command""", (h, h, h + 3600))
    for table in ("sample", "proc", "mem"):
        db.execute(f"DELETE FROM {table} WHERE ts < ?", (now - RAW_DAYS * 86400,))
    db.commit()


def scan_folders(path):
    """Own thread, own connection: `du` can take minutes. Sizes of each child of FOLDER_ROOTS, every 6 hours."""
    db = connect(path)
    while True:
        if time.time() - (db.execute("SELECT MAX(ts) FROM folder").fetchone()[0] or 0) >= FOLDER_EVERY_S:
            ts = time.time()
            for root in FOLDER_ROOTS:
                try:
                    out = subprocess.run(["du", "-kx", "-d", "1", str(root)], capture_output=True, text=True,
                                         errors="replace", timeout=300).stdout  # stderr: unreadable folders, ignored
                except subprocess.TimeoutExpired:
                    continue
                db.executemany("INSERT INTO folder VALUES(?,?,?)", [(ts, p, kb) for p, kb in parse_du(out, root)])
            db.commit()
        time.sleep(600)


def plist():
    log = str(APP_DIR / "log.txt")
    return {"Label": LABEL, "ProgramArguments": [sys.executable, str(APP_DIR / "app" / "slow.py"), "record"],
            "RunAtLoad": True, "KeepAlive": True, "Nice": 10, "StandardOutPath": log, "StandardErrorPath": log}


def install():
    connect().close()  # moves an old slow.db out of this folder now: the background recorder may not read it here
    src, app = Path(__file__).parent, APP_DIR / "app"
    app.mkdir(parents=True, exist_ok=True)
    for name in ("slow.py", "insights.py", "server.py"):
        shutil.copy(src / name, app)
    shutil.copytree(src / "web", app / "web", dirs_exist_ok=True)
    PLIST.parent.mkdir(exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist()))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)  # fails if not loaded: fine
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"Recording in the background from now on. Dashboard: http://127.0.0.1:{PORT}\n"
          "This runs a copy of the code: re-run install after git pull.")


def uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print(f"Background recorder removed. Your recorded data is kept in {APP_DIR}")


def mb(b):
    return f"{b / 1e6:,.0f} MB"


def hhmm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def culprits(db, t0, t1, n=3):
    rows = db.execute("""SELECT command, AVG(cpu), MAX(mem_mb) FROM proc WHERE ts BETWEEN ? AND ?
                         GROUP BY command ORDER BY AVG(cpu) DESC LIMIT ?""", (t0, t1, n)).fetchall()
    return ", ".join(f"{c} ({cpu:.0f}% CPU, {m:,.0f} MB)" for c, cpu, m in rows) or "no process data"


def deltas(prev, cur):
    """(seconds, swap-ins, disk bytes, network bytes) between two sample rows.
    The running totals restart at a reboot, so a negative difference counts as 0."""
    d = lambda *cols: max(0, sum(cur[i] - prev[i] for i in cols))
    return (cur[0] - prev[0]) or 1, d(6), d(9, 10), d(7, 8)


def spike_kinds(prev, cur):
    """Two consecutive sample rows -> {"cpu"|"mem"|"disk"|"net": description} for each limit crossed."""
    dt, swapins, disk, net = deltas(prev, cur)
    out = {}
    if cur[2] >= CPU_SPIKE:
        out["cpu"] = f"CPU {cur[2]:.0f}%"
    if swapins >= SWAPIN_SPIKE:
        out["mem"] = f"memory pressure ({swapins:,} swap-ins, {mb(cur[5])} free)"
    if disk / dt >= DISK_SPIKE:
        out["disk"] = f"disk {mb(disk / dt)}/s"
    if net / dt >= NET_SPIKE:
        out["net"] = f"network {mb(net / dt)}/s"
    return out


def spikes(rows):
    """rows: sample rows in time order -> [(ts, [reasons])]."""
    return [(cur[0], list(d.values())) for prev, cur in zip(rows, rows[1:]) if (d := spike_kinds(prev, cur))]


def why(minutes=30):
    db = connect()
    t1 = time.time()
    t0 = t1 - minutes * 60
    rows = db.execute("SELECT * FROM sample WHERE ts >= ? ORDER BY ts", (t0,)).fetchall()
    if len(rows) < 2:
        sys.exit(f"Not enough data in the last {minutes} min. Run `python3 slow.py record` first.")
    a, z = rows[0], rows[-1]
    total = lambda i: sum(max(0, q[i] - p[i]) for p, q in zip(rows, rows[1:]))  # survives a reboot mid-window
    print(f"== Last {minutes} min ({len(rows)} samples, {hhmm(a[0])} to {hhmm(z[0])}) ==")
    print(f"CPU avg {sum(r[2] for r in rows) / len(rows):.0f}%, max {max(r[2] for r in rows):.0f}%")
    print(f"Free memory min {mb(min(r[5] for r in rows))}, compressed max {mb(max(r[4] for r in rows))}, "
          f"swap-ins {total(6):,}")
    print(f"Disk read {mb(total(9))}, written {mb(total(10))}; network in {mb(total(7))}, out {mb(total(8))}")

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
    elif cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "why":
        why(int(args[0]) if args else 30)
    else:
        sys.exit(__doc__)
