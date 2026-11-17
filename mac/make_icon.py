"""App icon: a 16x16 pixel snail under a "?", scaled by whole numbers so every size stays crisp.

  python3 make_icon.py AppIcon.iconset    # then: iconutil -c icns AppIcon.iconset
"""
import struct
import sys
import zlib
from pathlib import Path

PALETTE = {".": "00000000", "O": "2a1b3dff", "S": "2a1552ff", "s": "5b2470ff", "W": "ffffffff", "B": "ff5d8fff",
           "Y": "ffcf4aff", "y": "a85a00ff", "G": "7fd066ff", "g": "6fc058ff"}
GRID = """
................
..OOOOOOOOOOOO..
.OSSSSSSSWWSSSO.
.OSSSSSSWSSWSSO.
.OSsSSSsSSWSSsO.
.OSSsSsSSsSSsSO.
.OsSsSsSsSWSsSO.
.OsWsWssOOOsSsO.
.OSBSBsOYYYOsSO.
.OsBBBOYyyYYOsO.
.OSBBBOYyYyYOSO.
.OsBBBBOYyyOBsO.
.OGBBBBBBBBBBGO.
.OgGgGgGgGgGgGO.
..OOOOOOOOOOOO..
................
""".split()
SIZES = (16, 32, 128, 256, 512)  # each also written at @2x


def png(px):
    """The grid as an RGBA PNG px pixels wide (a multiple of 16)."""
    k = px // 16
    rows = b"".join((b"\0" + b"".join(bytes.fromhex(PALETTE[c]) * k for c in row)) * k for row in GRID)  # 0: no filter
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def write_iconset(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        (folder / f"icon_{size}x{size}.png").write_bytes(png(size))
        (folder / f"icon_{size}x{size}@2x.png").write_bytes(png(size * 2))


if __name__ == "__main__":
    write_iconset(sys.argv[1])
