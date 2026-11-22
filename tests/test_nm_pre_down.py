"""The NetworkManager pre-down hook (#74).

AWDL must be off before Wi-Fi leaves a network: the hook ends any window,
which also cancels a start still in its gate, and only then turns AWDL off,
so that nothing turns it back on before the next join.

The hook is copied with its library path redirected to a temporary directory
holding stub omdrop-discoverable and brcm_iovar.py, with stub systemctl first
on PATH and a fake /sys/class/net. The stubs append to one log, so the order
of the calls can be checked. Nothing here runs as root or touches a radio.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "packaging" / "omdrop-awdl-nm-pre-down"

SYSTEMCTL = r'''#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB/calls.log"
exit 0
'''

DISCOVERABLE = r'''#!/usr/bin/env bash
echo "omdrop-discoverable $*" >> "$STUB/calls.log"
exit "$(cat "$STUB/stop.rc" 2>/dev/null || echo 0)"
'''

IOVAR = r'''import os, sys
stub = os.environ["STUB"]
with open(os.path.join(stub, "calls.log"), "a") as f:
    f.write("brcm_iovar " + " ".join(sys.argv[1:]) + "\n")
rc_file = os.path.join(stub, "set.rc")
sys.exit(int(open(rc_file).read()) if os.path.exists(rc_file) else 0)
'''

STOP = "omdrop-discoverable stop"
AWDL_OFF = "brcm_iovar -i awdl0 set awdl 00000000"


class PreDownHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        lib, bindir, self.sys = self.tmp / "lib", self.tmp / "bin", self.tmp / "sys"
        for d in (lib, bindir, self.sys / "phy0", self.sys / "phy1"):
            d.mkdir(parents=True)
        (lib / "omdrop-discoverable").write_text(DISCOVERABLE)
        (lib / "omdrop-discoverable").chmod(0o755)
        (lib / "brcm_iovar.py").write_text(IOVAR)
        (bindir / "systemctl").write_text(SYSTEMCTL)
        (bindir / "systemctl").chmod(0o755)
        self.hook = self.tmp / "hook"
        self.hook.write_text(HOOK.read_text().replace("LIB=/usr/lib/omdrop", f"LIB={lib}"))
        self.hook.chmod(0o755)
        self.env = dict(os.environ, STUB=str(self.tmp), AWDL_SYSFS=str(self.sys),
                        PATH=f"{bindir}:{os.environ['PATH']}")
        # wlan0 and awdl0 share phy0; wlan1 is another radio; enu1 is not Wi-Fi.
        self.netdev("wlan0", "phy0")
        self.netdev("awdl0", "phy0")
        self.netdev("wlan1", "phy1")
        self.netdev("enu1", None)

    def netdev(self, name, phy):
        (self.sys / name).mkdir()
        if phy:
            (self.sys / name / "phy80211").symlink_to(self.sys / phy)

    def run_hook(self, *args):
        result = subprocess.run([str(self.hook), *args], env=self.env,
                                capture_output=True, text=True, timeout=60)
        log = self.tmp / "calls.log"
        return result, log.read_text().splitlines() if log.exists() else []

    def test_window_ends_before_awdl_goes_off(self):
        result, calls = self.run_hook("wlan0", "pre-down")
        self.assertEqual(result.returncode, 0)
        self.assertIn(STOP, calls)
        self.assertIn(AWDL_OFF, calls)
        self.assertLess(calls.index(STOP), calls.index(AWDL_OFF))
        self.assertLess(calls.index("systemctl stop awdl-warmup.service"), calls.index(AWDL_OFF))

    def test_other_events_change_nothing(self):
        for event in ("up", "down", "pre-up", "connectivity-change", "dhcp4-change"):
            result, calls = self.run_hook("wlan0", event)
            self.assertEqual((result.returncode, calls), (0, []), event)

    def test_interfaces_off_the_awdl_radio_change_nothing(self):
        for iface in ("enu1", "wlan1", "gone0", ""):
            result, calls = self.run_hook(iface, "pre-down")
            self.assertEqual((result.returncode, calls), (0, []), repr(iface))

    def test_without_awdl0_nothing_changes(self):
        shutil.rmtree(self.sys / "awdl0")
        result, calls = self.run_hook("wlan0", "pre-down")
        self.assertEqual((result.returncode, calls), (0, []))

    def test_a_failed_stop_still_turns_awdl_off(self):
        (self.tmp / "stop.rc").write_text("1")
        result, calls = self.run_hook("wlan0", "pre-down")
        self.assertEqual(result.returncode, 0)
        self.assertIn(AWDL_OFF, calls)

    def test_a_refused_write_does_not_hold_up_the_switch(self):
        (self.tmp / "set.rc").write_text("1")
        result, _ = self.run_hook("wlan0", "pre-down")
        self.assertEqual(result.returncode, 0)
        self.assertIn("reload brcmfmac", result.stderr)


if __name__ == "__main__":
    unittest.main()
