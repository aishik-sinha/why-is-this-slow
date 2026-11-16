# Why is this slow?

A small macOS tool that watches your machine and tells you what was really going on when it slowed down.

It records CPU, memory pressure, disk and network activity, plus each app's CPU, memory and energy use, every 5 seconds. Afterwards it lines up each spike with the apps that were running at that moment.

No installs: Python 3 and the commands built into macOS (`top`, `ioreg`, `netstat`).

## Usage

```bash
python3 slow.py record             # leave running; Ctrl+C to stop
python3 slow.py mark "chrome froze" # run the moment it feels slow
python3 slow.py why 30             # explain the last 30 minutes
```

`why` shows:

- overall CPU, free memory, swap-ins, and disk and network totals
- the biggest CPU users and battery drains
- each spike (CPU, memory pressure, disk, network) with the apps running at that moment
- for each `mark`, what was running in the minute before and which spikes hit

History is stored in `slow.db` next to the script. It is git-ignored because it is a record of your own machine.

## Tests

```bash
python3 -m unittest test_slow
```

## Limits

- App names are cut to 16 characters by `top`.
- Spike thresholds (top of `slow.py`) are starting guesses; tune them for your machine.
- GPU use and heat throttling are not recorded, because macOS only shows them to admin users (`sudo powermetrics`).
