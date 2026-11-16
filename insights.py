"""Turns recorded history into plain-language verdicts. Pure functions of (db, t0, t1): easy to test.

Every insight returns {"verdict": str|None, "level": "ok"|"warn"|"bad"|"no_data", ...evidence}.
"""
import time
from bisect import bisect_left
from collections import Counter
from pathlib import Path

from slow import INTERVAL, RAW_DAYS, deltas

# Memory
MIN_SAMPLES = 60            # 5 minutes
LOW_FREE = 200 * 2**20      # bytes; under this the Mac is leaning on compression and swap
SWAP_WARN, SWAP_BAD = 1000, 5000  # swap-ins per hour
LOW_SHARE = 0.5
# Battery drains
MIN_HOURS = 24
HOG_WARN, HOG_BAD = (2, 0.25), (5, 0.5)  # (average energy impact, share of hours it was active)
# Launches
LAUNCH_WINDOW = 60          # seconds after a launch in which a spike counts against the app
MIN_LAUNCHES = 3
CHOKE_RATE = 0.5
LIFT = 2                    # ...and that often compared with any random minute
# Disk
DISK_DAYS = 7               # window for the fill rate
DISK_BAD_FREE = 10e9
DISK_BAD_DAYS, DISK_WARN_DAYS = 30, 90

KIND = {"cpu": "a CPU spike", "mem": "memory pressure", "disk": "heavy disk use", "net": "heavy network use"}
UNEXPLAINED = "No spike. The cause may be outside what this records (GPU, heat throttling, one app hanging)."


def no_data(need):
    return {"verdict": None, "level": "no_data", "need": need}


def gb(b):
    return f"{b / 1e9:,.1f} GB"


def memory(db, t0, t1):
    t0 = max(t0, t1 - RAW_DAYS * 86400)  # needs per-sample rows
    rows = db.execute("SELECT * FROM sample WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1)).fetchall()
    if len(rows) < MIN_SAMPLES:
        return no_data("5 minutes of recording")
    low_share = sum(r[5] < LOW_FREE for r in rows) / len(rows)
    sph = sum(deltas(a, b)[1] for a, b in zip(rows, rows[1:])) / (len(rows) * INTERVAL / 3600)
    groups = db.execute("""SELECT name, AVG(rss_mb) FROM mem WHERE ts BETWEEN ? AND ?
                           GROUP BY name ORDER BY 2 DESC LIMIT 5""", (t0, t1)).fetchall()
    if sph >= SWAP_BAD or (low_share >= LOW_SHARE and sph >= SWAP_WARN):
        level, lead = "bad", "RAM is the bottleneck"
    elif sph >= SWAP_WARN or low_share >= LOW_SHARE:
        level, lead = "warn", "Memory is tight"
    else:
        level, lead = "ok", "Memory is fine"
    holders = ", ".join(f"{name} {m / 1024:.1f} GB" for name, m in groups[:3])
    return {"level": level,
            "verdict": f"{lead}: under 200 MB free {low_share:.0%} of the time, {sph:,.0f} swap-ins/hour."
                       + (f" Biggest holders: {holders}." if holders else ""),
            "median_free": sorted(r[5] for r in rows)[len(rows) // 2], "max_compressed": max(r[4] for r in rows),
            "swapins_per_hour": sph, "low_share": low_share,
            "groups": [{"name": name, "rss_mb": m} for name, m in groups]}


def hogs(db, t0, t1, n=10):
    """Steady battery drains: energy impact averaged over ALL recorded time, so a short burst ranks low."""
    span = (t0 - 3600, t1)
    hours, samples = db.execute("SELECT COUNT(*), SUM(n) FROM sample_hour WHERE hour > ? AND hour < ?", span).fetchone()
    if hours < MIN_HOURS:
        return no_data(f"{MIN_HOURS} recorded hours (have {hours})")
    rows = [{"command": c, "score": score, "cpu_avg": cpu, "presence": active / hours} for c, score, cpu, active in db.execute(
        """SELECT command, SUM(power_sum) * 1.0 / ?1, SUM(cpu_sum) * 1.0 / ?1, SUM(power_sum > 0) FROM proc_hour
           WHERE hour > ?2 AND hour < ?3 AND command != 'top' GROUP BY command ORDER BY 2 DESC LIMIT ?4""",
        (samples, *span, n))]
    out = {"level": "ok", "verdict": "No app is a steady battery drain.", "hours": hours, "rows": rows}
    if rows:
        top = rows[0]
        for level, (score, presence) in (("bad", HOG_BAD), ("warn", HOG_WARN)):
            if top["score"] >= score and top["presence"] >= presence:
                out["level"] = level
                out["verdict"] = (f"{top['command']} has used energy in {top['presence']:.0%} of the hours over "
                                  f"{max(1, round(hours / 24))} days (avg impact {top['score']:.1f}).")
                break
    return out


def launches(db, t0, t1):
    """Apps whose launch is usually followed by a spike, and far more often than a random minute is."""
    by_app = {}
    for ts, app in db.execute("SELECT ts, app FROM launch WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1)):
        by_app.setdefault(app, []).append(ts)
    spikes = db.execute("SELECT ts, kinds, culprit FROM spike WHERE ts BETWEEN ? AND ? ORDER BY ts",
                        (t0, t1 + LAUNCH_WINDOW)).fetchall()
    samples = db.execute("""SELECT (SELECT COALESCE(SUM(n), 0) FROM sample_hour WHERE hour > ?1 - 3600 AND hour < ?2)
        + (SELECT COUNT(*) FROM sample WHERE ts BETWEEN ?1 AND ?2  -- raw rows not rolled up yet
           AND ts >= COALESCE((SELECT MAX(hour) + 3600 FROM sample_hour), 0))""", (t0, t1)).fetchone()[0]
    if not by_app or not samples:
        return no_data(f"an app opened {MIN_LAUNCHES} times while recording")
    baseline = min(1, len({s[0] // 60 for s in spikes if s[0] <= t1}) / (samples * INTERVAL / 60))
    times, rows = [s[0] for s in spikes], []
    for app, starts in by_app.items():
        kinds, culprits, choked = Counter(), Counter(), 0
        for ts in starts:
            i = bisect_left(times, ts)
            after = spikes[i:bisect_left(times, ts + LAUNCH_WINDOW + 1e-9, i)]
            choked += bool(after)
            kinds.update(k for s in after for k in s[1].split(","))
            culprits.update(s[2] for s in after)
        rate = choked / len(starts)
        row = {"app": app, "launches": len(starts), "choked": choked, "rate": rate, "baseline": baseline,
               "kind": kinds.most_common(1)[0][0] if kinds else None,
               "culprit": culprits.most_common(1)[0][0] if culprits else None,
               "flagged": len(starts) >= MIN_LAUNCHES and rate >= CHOKE_RATE and (baseline == 0 or rate >= LIFT * baseline)}
        if row["flagged"]:
            lift = f", {rate / baseline:.0f}x your normal rate" if baseline else ""
            row["verdict"] = (f"{'Every time' if choked == len(starts) else 'When'} {app} opens ({choked} of {len(starts)} "
                              f"launches) the Mac hits {KIND.get(row['kind'], 'a spike')} within a minute{lift}. "
                              f"Culprit then: {row['culprit']}.")
        rows.append(row)
    rows.sort(key=lambda r: (-r["flagged"], -r["rate"] * r["launches"]))
    flagged = [r["verdict"] for r in rows if r["flagged"]]
    return {"level": "warn" if flagged else "ok",
            "verdict": " ".join(flagged[:3]) or "No app chokes the machine when it opens.", "rows": rows[:10]}


def disk(db, t0, t1):
    """Fill rate over the last DISK_DAYS (whatever the window); folder growth over the window."""
    now = db.execute("SELECT ts, free FROM disk_free WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (t1,)).fetchone()
    old = db.execute("SELECT ts, free FROM disk_free WHERE ts >= ? ORDER BY ts LIMIT 1", (t1 - DISK_DAYS * 86400,)).fetchone()
    if not now or not old or (days := (now[0] - old[0]) / 86400) < 1:
        return no_data("1 day of recording")
    free, per_day = now[1], (old[1] - now[1]) / days
    days_left = free / per_day if per_day > 0 else None
    window = "FROM folder WHERE ts BETWEEN ? AND ? GROUP BY path"  # SQLite: kb comes from the MIN/MAX(ts) row
    first = {p: kb for p, kb, _ in db.execute(f"SELECT path, kb, MIN(ts) {window}", (t0, t1))}
    last = db.execute(f"SELECT path, kb, MAX(ts) {window}", (t0, t1)).fetchall()
    home = str(Path.home())
    folders = sorted(({"path": p.replace(home, "~", 1), "growth": (kb - first[p]) * 1024, "size": kb * 1024}
                      for p, kb, _ in last if kb > first[p]), key=lambda f: -f["growth"])[:10]
    if free < DISK_BAD_FREE or (days_left and days_left < DISK_BAD_DAYS):
        level = "bad"
    else:
        level = "warn" if days_left and days_left < DISK_WARN_DAYS else "ok"
    verdict = f"{gb(free)} free. " + (f"Losing {gb(per_day)}/day; full in ~{days_left:,.0f} days." if days_left
                                      else "Not filling up.")
    if folders:
        verdict += f" Fastest growing: {folders[0]['path']} +{gb(folders[0]['growth'])}."
    return {"level": level, "verdict": verdict, "free": free, "per_day": per_day, "days_left": days_left, "folders": folders}


def procs_at(db, t0, t1, n=10):
    """Top processes in [t0, t1] -> ("raw"|"hour", rows). Falls back to that hour's averages once raw rows are gone."""
    rows = db.execute("""SELECT command, AVG(cpu), MAX(mem_mb), AVG(power) FROM proc WHERE ts BETWEEN ? AND ?
                         GROUP BY command ORDER BY 2 DESC LIMIT ?""", (t0, t1, n)).fetchall()
    resolution = "raw"
    if not rows:
        resolution = "hour"
        rows = db.execute("""SELECT command, cpu_sum * 1.0 / n, mem_max, power_sum * 1.0 / n FROM proc_hour JOIN sample_hour USING(hour)
                             WHERE hour = ? ORDER BY 2 DESC LIMIT ?""", ((t0 + t1) / 2 // 3600 * 3600, n)).fetchall()
    return resolution, [dict(zip(("command", "cpu", "mem_mb", "power"), r)) for r in rows]


def marks(db, t0, t1):
    """For each moment you marked as slow: the spikes around it and what was running."""
    rows = []
    for ts, note in db.execute("SELECT ts, note FROM mark WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 50", (t0, t1)):
        found = db.execute("SELECT detail, culprit FROM spike WHERE ts BETWEEN ? AND ? ORDER BY ts",
                           (ts - 60, ts + INTERVAL)).fetchall()
        details = list(dict.fromkeys(part.split(" (")[0] for d, _ in found for part in d.split("; ")))
        rows.append({"ts": ts, "note": note, "explained": bool(found), "procs": procs_at(db, ts - 60, ts + INTERVAL, 5)[1],
                     "verdict": f"{', '.join(details)}. Culprit: {Counter(c for _, c in found).most_common(1)[0][0]}."
                                if found else UNEXPLAINED})
    if not rows:
        return no_data('a mark: press "It\'s slow now" the moment it lags')
    hit = sum(r["explained"] for r in rows)
    return {"level": "ok" if hit == len(rows) else "warn", "rows": rows,
            "verdict": f"{hit} of {len(rows)} marks line up with a recorded spike."}


def summary(db, days, now=None):
    t1 = now or time.time()
    t0 = t1 - days * 86400
    out = {name: f(db, t0, t1) for name, f in (("memory", memory), ("hogs", hogs), ("launches", launches),
                                                 ("disk", disk), ("marks", marks))}
    # marks is a list of your own notes, not a finding about the machine, so it never makes a headline
    found = [v for k, v in out.items() if k != "marks"]
    return {"days": days, "headlines": [v["verdict"] for level in ("bad", "warn") for v in found if v["level"] == level], **out}
