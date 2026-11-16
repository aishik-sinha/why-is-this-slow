import unittest

from slow import parse_block, parse_counters, spikes

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


if __name__ == "__main__":
    unittest.main()
