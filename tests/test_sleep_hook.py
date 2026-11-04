"""The systemd-sleep hook (#67): no window and no awdl0 across a suspend.

The hook is copied with its library path redirected to a temporary directory
holding stub omdrop-discoverable and brcm_iovar.py, with stub ip and sleep
first on PATH. Every stub appends to one log, so the order of the calls can
be checked. Nothing here runs as root or touches a radio.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "packaging" / "omdrop-awdl-sleep"

# awdl0 exists while $STUB/awdl0 does.
IP = r'''#!/usr/bin/env bash
echo "ip $*" >> "$STUB/calls.log"
[[ $* == "link show awdl0" && ! -e $STUB/awdl0 ]] && exit 1
exit 0
'''

DISCOVERABLE = r'''#!/usr/bin/env bash
echo "omdrop-discoverable $*" >> "$STUB/calls.log"
exit "$(cat "$STUB/stop.rc" 2>/dev/null || echo 0)"
'''

# The destroy request removes awdl0 unless destroy.rc says it failed.
IOVAR = r'''import os, sys
stub = os.environ["STUB"]
with open(os.path.join(stub, "calls.log"), "a") as f:
    f.write("brcm_iovar " + " ".join(sys.argv[1:]) + "\n")
rc_file = os.path.join(stub, "destroy.rc")
rc = int(open(rc_file).read()) if os.path.exists(rc_file) else 0
if rc:
    sys.exit("awdl-if destroy -> ENODEV")
if os.path.exists(os.path.join(stub, "awdl0")):
    os.remove(os.path.join(stub, "awdl0"))
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


class SleepHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.stub = root / "stub"
        self.stub.mkdir()
        lib = root / "lib"
        lib.mkdir()
        executable(lib / "omdrop-discoverable", DISCOVERABLE)
        executable(lib / "brcm_iovar.py", IOVAR)
        bin_ = root / "bin"
        bin_.mkdir()
        executable(bin_ / "ip", IP)
        executable(bin_ / "sleep", "#!/usr/bin/env bash\nexit 0\n")
        text = HOOK.read_text()
        self.assertIn("\nLIB=/usr/lib/omdrop\n", text)
        self.hook = root / "omdrop-awdl"
        executable(self.hook, text.replace("\nLIB=/usr/lib/omdrop\n", f"\nLIB={lib}\n"))
        self.env = dict(os.environ, STUB=str(self.stub), PATH=f"{bin_}:{os.environ['PATH']}")

    def run_hook(self, *args):
        return subprocess.run([str(self.hook), *args], capture_output=True, text=True,
                              env=self.env, timeout=30, cwd="/")

    def calls(self):
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_before_sleep_stops_the_window_then_downs_and_destroys_awdl0(self):
        (self.stub / "awdl0").touch()
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], "omdrop-discoverable stop")
        down = calls.index("ip link set awdl0 down")
        destroy = calls.index("brcm_iovar -i awdl0 awdl-if destroy")
        self.assertLess(down, destroy)
        self.assertFalse((self.stub / "awdl0").exists())
        self.assertIn("removed awdl0", result.stderr)

    def test_resume_does_nothing(self):
        (self.stub / "awdl0").touch()
        result = self.run_hook("post", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.stub / "awdl0").exists())

    def test_without_awdl0_only_the_window_is_stopped(self):
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["omdrop-discoverable stop", "ip link show awdl0"])

    def test_failures_are_logged_and_never_block_sleep(self):
        (self.stub / "awdl0").touch()
        (self.stub / "stop.rc").write_text("3")
        (self.stub / "destroy.rc").write_text("1")
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("omdrop-discoverable stop exited 3", result.stderr)
        self.assertIn("destroying awdl0 failed: awdl-if destroy -> ENODEV", result.stderr)
        self.assertTrue((self.stub / "awdl0").exists())


if __name__ == "__main__":
    unittest.main()
