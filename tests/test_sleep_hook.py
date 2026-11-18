"""The systemd-sleep hook and awdl-up's side of it (#67).

No window and no awdl0 across a suspend: the hook ends the window and removes
awdl0, and no create may bring awdl0 back before resume. Every awdl0 is
created by awdl-up under its create lock, and awdl-up refuses while the sleep
marker exists, so the hook writes the marker, waits for a create in flight,
then tears down.

The hook is copied with its library path redirected to a temporary directory
holding stub omdrop-discoverable and brcm_iovar.py, with stub ip, sleep,
nmcli and systemctl first on PATH. The stubs append to one log, so the order
of the calls can be checked. Nothing here runs as root or touches a radio:
awdl-up, which refuses to run without root, runs in a user namespace.
"""
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "packaging" / "omdrop-awdl-sleep"
AWDL_UP = ROOT / "userspace" / "awdl-up"

# awdl0 exists while $STUB/awdl0 does; wlan0 always does. `ip -d link show
# wlan0` writes the sleep marker when $STUB/marker-on-permaddr exists, which
# is a hook arriving while awdl-up waits for the permanent MAC.
IP = r'''#!/usr/bin/env bash
echo "ip $*" >> "$STUB/calls.log"
case "$*" in
  "link show awdl0") [[ -e $STUB/awdl0 ]] || exit 1 ;;
  "-d link show wlan0") [[ -e $STUB/marker-on-permaddr ]] && : > "$AWDL_SLEEP_MARKER" ;;
esac
exit 0
'''

DISCOVERABLE = r'''#!/usr/bin/env bash
echo "omdrop-discoverable $*" >> "$STUB/calls.log"
exit "$(cat "$STUB/stop.rc" 2>/dev/null || echo 0)"
'''

# destroy removes awdl0 unless destroy.rc says it failed; create makes it;
# anything else succeeds.
IOVAR = r'''import os, sys
stub = os.environ["STUB"]
with open(os.path.join(stub, "calls.log"), "a") as f:
    f.write("brcm_iovar " + " ".join(sys.argv[1:]) + "\n")
awdl0 = os.path.join(stub, "awdl0")
if "--raw" in sys.argv:
    # A GET writes the bytes of get.<iovar>, or nothing (length 0).
    src = os.path.join(stub, "get." + sys.argv[sys.argv.index("get") + 1])
    with open(sys.argv[sys.argv.index("--raw") + 1], "wb") as out:
        if os.path.exists(src):
            out.write(open(src, "rb").read())
elif sys.argv[-1] == "create":
    open(awdl0, "w").close()
elif sys.argv[-1] == "destroy":
    rc_file = os.path.join(stub, "destroy.rc")
    if os.path.exists(rc_file) and int(open(rc_file).read()):
        sys.exit("awdl-if destroy -> ENODEV")
    if os.path.exists(awdl0):
        os.remove(awdl0)
'''

# A create in flight: takes the create lock, says so, and after HOLD seconds
# creates awdl0 and lets go -- or holds it until killed when HOLD is "forever".
# "forking" also keeps forking children that inherit the lock, as awdl-up
# does for each command it runs, so a holder can appear between a scan and
# its kill.
CREATOR = r'''import fcntl, os, sys, time
stub, lock, hold = os.environ["STUB"], sys.argv[1], sys.argv[2]
fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
open(os.path.join(stub, "creator.locked"), "w").close()
if hold == "forking":
    while True:
        if os.fork() == 0:
            while True:
                time.sleep(60)
        time.sleep(0.005)
if hold == "forever":
    while True:
        time.sleep(60)
time.sleep(float(hold))
open(os.path.join(stub, "awdl0"), "w").close()
'''


# A transmit gate in flight: holds the start lock, says so, and after HOLD
# seconds turns AWDL on (the SET a review reproduced landing between the
# hook's awdl=0 and its destroy), then lets go.
GATE = r'''import fcntl, os, sys, time
stub, lock, hold = os.environ["STUB"], sys.argv[1], float(sys.argv[2])
fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
open(os.path.join(stub, "gate.locked"), "w").close()
time.sleep(hold)
with open(os.path.join(stub, "calls.log"), "a") as f:
    f.write("gate set awdl 01000000\n")
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


def userns_root():
    try:
        out = subprocess.run(["unshare", "-r", "id", "-u"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.strip() == "0"


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.stub = root / "stub"
        self.stub.mkdir()
        self.lib = root / "lib"
        self.lib.mkdir()
        executable(self.lib / "omdrop-discoverable", DISCOVERABLE)
        executable(self.lib / "brcm_iovar.py", IOVAR)
        bin_ = root / "bin"
        bin_.mkdir()
        executable(bin_ / "ip", IP)
        executable(bin_ / "sleep", "#!/usr/bin/env bash\nexit 0\n")
        executable(bin_ / "nmcli", "#!/usr/bin/env bash\nexit 0\n")
        executable(bin_ / "systemctl", "#!/usr/bin/env bash\nexit 1\n")
        text = HOOK.read_text()
        self.assertIn("\nLIB=/usr/lib/omdrop\n", text)
        self.hook = root / "omdrop-awdl"
        executable(self.hook, text.replace("\nLIB=/usr/lib/omdrop\n", f"\nLIB={self.lib}\n"))
        self.marker = self.stub / "sleeping"
        self.lock = self.stub / "create.lock"
        self.env = dict(os.environ, STUB=str(self.stub), PATH=f"{bin_}:{os.environ['PATH']}",
                        AWDL_SLEEP_MARKER=str(self.marker), AWDL_CREATE_LOCK=str(self.lock),
                        AWDL_START_LOCK=str(self.stub / "start.lock"))

    def run_hook(self, *args, **env):
        return subprocess.run([str(self.hook), *args], capture_output=True, text=True,
                              env=dict(self.env, **env), timeout=30, cwd="/")

    def calls(self):
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def creator(self, hold):
        proc = subprocess.Popen([sys.executable, "-c", CREATOR, str(self.lock), hold],
                                env=self.env, start_new_session=True)
        self.addCleanup(self.kill_group, proc)
        deadline = time.monotonic() + 10
        while not (self.stub / "creator.locked").exists():
            self.assertLess(time.monotonic(), deadline, "the creator never took the lock")
            time.sleep(0.01)
        return proc

    @staticmethod
    def kill_group(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)

    def lock_is_free(self):
        fd = os.open(self.lock, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)


class SleepHookTests(Harness):
    def test_before_sleep_marks_stops_the_window_then_downs_and_destroys_awdl0(self):
        (self.stub / "awdl0").touch()
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        calls = self.calls()
        self.assertEqual(calls[0], "omdrop-discoverable stop")
        self.assertLess(calls.index("ip link set awdl0 down"),
                        calls.index("brcm_iovar -i awdl0 set awdl 00000000"))
        self.assertLess(calls.index("brcm_iovar -i awdl0 set awdl 00000000"),
                        calls.index("brcm_iovar -i awdl0 awdl-if destroy"))
        self.assertFalse((self.stub / "awdl0").exists())
        self.assertIn("removed awdl0", result.stderr)

    def test_with_a_template_loaded_awdl0_is_still_removed(self):
        # The state any window leaves behind (#67).
        (self.stub / "awdl0").touch()
        (self.stub / "get.awdl_payload").write_bytes(b"\0" * 512)
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("brcm_iovar -i awdl0 set awdl 00000000"),
                        calls.index("brcm_iovar -i awdl0 awdl-if destroy"))
        self.assertFalse((self.stub / "awdl0").exists())

    def test_resume_only_clears_the_marker(self):
        self.marker.touch()
        (self.stub / "awdl0").touch()
        result = self.run_hook("post", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.stub / "awdl0").exists())

    def test_without_awdl0_only_the_window_is_stopped(self):
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["omdrop-discoverable stop", "ip link show awdl0"])

    def test_a_create_in_flight_lands_first_and_is_then_removed(self):
        # The reproduced #69 review failure: the hook found no awdl0 and
        # exited, and a create already under way brought it back afterwards.
        creator = self.creator("0.5")
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(creator.wait(timeout=10), 0)
        self.assertIn("brcm_iovar -i awdl0 awdl-if destroy", self.calls())
        self.assertFalse((self.stub / "awdl0").exists())

    def test_a_gate_in_flight_finishes_before_awdl_goes_off(self):
        (self.stub / "awdl0").touch()
        gate = subprocess.Popen([sys.executable, "-c", GATE, str(self.stub / "start.lock"), "0.5"],
                                env=self.env, start_new_session=True)
        self.addCleanup(self.kill_group, gate)
        deadline = time.monotonic() + 10
        while not (self.stub / "gate.locked").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("gate set awdl 01000000"),
                        calls.index("brcm_iovar -i awdl0 set awdl 00000000"))
        self.assertFalse((self.stub / "awdl0").exists())

    def test_a_link_planted_at_the_start_lock_is_not_written_through(self):
        # A run directory left writable by an older helper (Greptile on #69).
        run = self.stub / "run"
        run.mkdir()
        run.chmod(0o777)
        victim = self.stub / "victim"
        victim.write_text("keep me\n")
        (run / "start.lock").symlink_to(victim)
        result = self.run_hook("pre", "suspend", AWDL_START_LOCK=str(run / "start.lock"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(victim.read_text(), "keep me\n")
        self.assertFalse((run / "start.lock").is_symlink())
        self.assertEqual(run.stat().st_mode & 0o022, 0)

    def test_a_create_that_never_lets_go_is_killed_and_sleep_goes_ahead(self):
        creator = self.creator("forever")
        result = self.run_hook("pre", "suspend", AWDL_SLEEP_CREATE_WAIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(creator.wait(timeout=10), -signal.SIGKILL)
        self.assertIn(f"killing {creator.pid}", result.stderr)
        self.assertEqual(self.calls()[0], "omdrop-discoverable stop")

    def test_children_forked_between_scan_and_kill_are_killed_too(self):
        creator = self.creator("forking")
        result = self.run_hook("pre", "suspend", AWDL_SLEEP_CREATE_WAIT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("still held", result.stderr)
        self.assertEqual(creator.wait(timeout=10), -signal.SIGKILL)
        self.assertTrue(self.lock_is_free(), "a forked holder survived the hook")

    def test_failures_are_logged_and_never_block_sleep(self):
        (self.stub / "awdl0").touch()
        (self.stub / "stop.rc").write_text("3")
        (self.stub / "destroy.rc").write_text("1")
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("omdrop-discoverable stop exited 3", result.stderr)
        self.assertIn("destroying awdl0 failed: awdl-if destroy -> ENODEV", result.stderr)
        self.assertTrue((self.stub / "awdl0").exists())


@unittest.skipUnless(shutil.which("unshare") and userns_root(),
                     "awdl-up needs root; no unprivileged user namespace here")
class AwdlUpWhileSleepingTests(Harness):
    def setUp(self):
        super().setUp()
        shutil.copy2(AWDL_UP, self.lib / "awdl-up")
        shutil.copy2(AWDL_UP.parent / "awdl_schedule.py", self.lib / "awdl_schedule.py")
        # The Wi-Fi interface is associated on channel 44, which the default sequence visits.
        bin_ = Path(self.env["PATH"].split(":")[0])
        executable(bin_ / "iw", "#!/usr/bin/env bash\nprintf '%s\\n' 'Connected to c6:4f:d5:41:10:72 (on wlan0)' $'\\tfreq: 5220.0'\n")
        self.env.update(AWDL_STA="wlan0", AWDL_UP_LOCK=str(self.stub / "awdl-up.lock"),
                        OMDROP_CONF=str(self.stub / "conf"))

    def awdl_up(self):
        return subprocess.run(["unshare", "-r", str(self.lib / "awdl-up")], capture_output=True,
                              text=True, env=self.env, timeout=60, cwd="/")

    def test_no_create_while_the_marker_exists(self):
        self.marker.touch()
        result = self.awdl_up()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertNotIn("brcm_iovar -i wlan0 awdl-if create", self.calls())
        self.assertFalse((self.stub / "awdl0").exists())

    def test_a_marker_written_before_the_create_stops_it(self):
        # The hook arrives while awdl-up waits for the permanent MAC, after
        # its first look at the marker.
        (self.stub / "marker-on-permaddr").touch()
        result = self.awdl_up()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("not creating awdl0", result.stdout)
        self.assertNotIn("brcm_iovar -i wlan0 awdl-if create", self.calls())

    def test_without_the_marker_awdl0_is_created(self):
        result = self.awdl_up()
        self.assertIn("brcm_iovar -i wlan0 awdl-if create", self.calls(), result.stdout)
        self.assertTrue((self.stub / "awdl0").exists())


if __name__ == "__main__":
    unittest.main()
