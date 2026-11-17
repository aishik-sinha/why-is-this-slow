import os
import plistlib
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from mac import make_icon

APP = Path(__file__).with_name("build") / "Why is this slow?.app"


class AppTest(unittest.TestCase):
    def test_icon_grid(self):
        self.assertEqual([len(row) for row in make_icon.GRID], [16] * 16)
        self.assertLessEqual(set("".join(make_icon.GRID)), set(make_icon.PALETTE))
        with tempfile.TemporaryDirectory() as tmp:
            make_icon.write_iconset(tmp)
            self.assertEqual(len(list(Path(tmp).glob("icon_*.png"))), 10)
            head = (Path(tmp) / "icon_512x512@2x.png").read_bytes()[:24]
            self.assertEqual(struct.unpack(">II", head[16:]), (1024, 1024))  # IHDR width, height

    @unittest.skipUnless(APP.exists(), "run ./build_app.sh first")
    def test_bundle(self):
        c = APP / "Contents"
        info = plistlib.loads((c / "Info.plist").read_bytes())
        want = {"CFBundleIdentifier": "com.github.aishik-sinha.why-is-this-slow.app", "CFBundleName": "Why is this slow?",
                "CFBundleDisplayName": "Why is this slow?", "CFBundleExecutable": "WhyIsThisSlow",
                "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0", "CFBundleDevelopmentRegion": "en",
                "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1", "CFBundleIconFile": "AppIcon",
                "LSMinimumSystemVersion": "13.0", "LSApplicationCategoryType": "public.app-category.utilities",
                "NSPrincipalClass": "NSApplication", "NSHighResolutionCapable": True,
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}
        self.assertEqual(info, want)  # exact: no LSUIElement, no NSAllowsArbitraryLoads
        self.assertTrue(os.access(c / "MacOS" / "WhyIsThisSlow", os.X_OK))
        for name in ("slow.py", "server.py", "insights.py", "web/index.html", "AppIcon.icns"):
            self.assertTrue((c / "Resources" / name).is_file(), name)
        self.assertEqual(list(APP.rglob("__pycache__")), [])  # would break the signature
        with tempfile.TemporaryDirectory() as tmp:
            # checked on a copy without extended attributes: under a cloud-synced Desktop the sync service
            # stamps Finder info onto the bundle, which --strict rejects although the signature is intact
            copy = str(Path(tmp) / APP.name)
            subprocess.run(["ditto", "--noextattr", "--norsrc", str(APP), copy], check=True)
            self.assertEqual(subprocess.run(["codesign", "--verify", "--strict", copy]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
