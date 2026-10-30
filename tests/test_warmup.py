"""awdl-warmup: create awdl0, run the gate once, leave awdl enabled."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

USERSPACE = Path(__file__).resolve().parent.parent / "userspace"

# Stands in for omdrop-broadcom.sh. Each function the warm-up calls records
# itself, and the test sets what awdl0_ready, iovar_len and gate answer.
LIBRARY = r'''
RUN=$STUB/run
log(){ printf '%s\n' "$*" >> "$STUB/calls"; printf '%s\n' "$*" >&2; }
die(){ local rc=$1; shift; log "$*"; exit "$rc"; }
now(){ date +%s; }
hook_mode(){ :; }
need_root(){ :; }
need_run_dir(){ mkdir -p "$RUN"; }
awdl0_ready(){ [[ -e $STUB/awdl0 ]]; }
iovar_len(){ echo "${STUB_PLEN:-0}"; }
iovar_set(){ log "iovar_set $*"; }
gate(){ log gate; return "${STUB_GATE_RC:-0}"; }
'''

# Stands in for awdl-up: records that it ran and, unless told otherwise,
# creates awdl0.
AWDL_UP = r'''#!/usr/bin/env bash
echo awdl-up >> "$STUB/calls"
[[ ${STUB_NO_AWDL0:-} ]] || touch "$STUB/awdl0"
'''


class WarmupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = self.root / "lib"
        self.stub = self.root / "stub"
        self.lib.mkdir()
        self.stub.mkdir()
        (self.lib / "omdrop-broadcom.sh").write_text(LIBRARY)
        for name, body in (("awdl-warmup", (USERSPACE / "awdl-warmup").read_text()),
                           ("awdl-up", AWDL_UP)):
            path = self.lib / name
            path.write_text(body)
            path.chmod(0o755)

    def run_warmup(self, **env):
        full = dict(os.environ, STUB=str(self.stub), AWDL_WARMUP_LOCK_WAIT="1",
                    AWDL_UP_LOCK=str(self.stub / "awdl-up.lock"), **env)
        return subprocess.run([str(self.lib / "awdl-warmup")], capture_output=True,
                              text=True, env=full, timeout=20, cwd="/")

    def calls(self):
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_creates_awdl0_then_runs_the_gate_and_leaves_awdl_enabled(self):
        result = self.run_warmup()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("awdl-up"), calls.index("gate"))
        self.assertFalse([c for c in calls if c.startswith("iovar_set")],
                         "awdl must stay enabled after the warm-up")

    def test_an_existing_awdl0_is_not_recreated(self):
        (self.stub / "awdl0").touch()
        self.run_warmup()
        self.assertNotIn("awdl-up", self.calls())
        self.assertIn("gate", self.calls())

    def test_a_loaded_template_skips_the_gate(self):
        # A window already ran, and the gate must not retoggle awdl with a
        # template loaded.
        (self.stub / "awdl0").touch()
        result = self.run_warmup(STUB_PLEN="512")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("gate", self.calls())

    def test_the_gate_waits_for_an_awdl_up_still_configuring_awdl0(self):
        # awdl0.service's awdl-up may have created awdl0 and still be writing
        # the schedule when the warm-up starts.
        (self.stub / "awdl0").touch()
        lock = self.stub / "awdl-up.lock"
        holder = subprocess.Popen(["flock", str(lock), "sh", "-c",
                                   f"sleep 1; echo awdl0.service-done >> {self.stub}/calls"])
        self.addCleanup(holder.wait)
        deadline = time.monotonic() + 5
        while not subprocess.run(["flock", "-n", str(lock), "true"]).returncode:
            self.assertLess(time.monotonic(), deadline, "the holder never took the lock")
            time.sleep(0.05)
        result = self.run_warmup()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("awdl0.service-done"), calls.index("gate"))

    def test_no_awdl0_exits_4_without_the_gate(self):
        result = self.run_warmup(STUB_NO_AWDL0="1")
        self.assertEqual(result.returncode, 4)
        self.assertNotIn("gate", self.calls())

    def test_the_gate_failing_is_the_exit_status(self):
        result = self.run_warmup(STUB_GATE_RC="1")
        self.assertEqual(result.returncode, 1)

    def test_a_held_start_lock_skips_the_warmup(self):
        run = self.stub / "run"
        run.mkdir()
        holder = subprocess.Popen(["flock", str(run / "start.lock"), "sleep", "10"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        for _ in range(50):
            if subprocess.run(["flock", "-n", str(run / "start.lock"), "true"]).returncode:
                break
            subprocess.run(["sleep", "0.05"])
        result = self.run_warmup()
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("gate", self.calls())


DISCOVERABLE = USERSPACE / "omdrop-discoverable"
BROADCOM = USERSPACE / "omdrop-broadcom.sh"


def function(source, name):
    start = source.index(f"\n{name}(){{") + 1
    return source[start:source.index("\n}\n", start) + 3]


class StartWaitsForWarmupTests(unittest.TestCase):
    """A window opened during the boot warm-up waits for it instead of failing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        self.run.mkdir()
        (self.run / "until").write_text("0")
        source = DISCOVERABLE.read_text() + BROADCOM.read_text()
        self.fragments = "\n".join(function(source, n) for n in
                                   ("is_uint", "is_duration", "warmup_running", "cmd_start"))

    def hold_lock(self, as_warmup, seconds="1.5"):
        # A process named awdl-warmup holds the start lock, recorded in
        # $RUN/warmup as the warm-up records itself, or an unrelated one does.
        name = self.root / ("awdl-warmup" if as_warmup else "other-start")
        name.write_text(f'#!/usr/bin/env bash\nexec 9>"{self.run}/start.lock"\nflock 9\n'
                        f'echo $$ > "{self.run}/warmup"\nsleep {seconds}\n')
        name.chmod(0o755)
        holder = subprocess.Popen([str(name)])
        self.addCleanup(holder.wait)
        deadline = time.monotonic() + 5
        while not (self.run / "warmup").exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def start(self):
        script = f'''
set -uo pipefail
RUN={self.run}
WARMUP_WAIT=10
log(){{ printf '%s\\n' "$*" >&2; }}
die(){{ local rc=$1; shift; log "$*"; exit "$rc"; }}
need_root(){{ :; }}
now(){{ date +%s; }}
core_supervisor_pids(){{ :; }}
supervisor_alive(){{ return 0; }}
{self.fragments}
cmd_start 600
'''
        began = time.monotonic()
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        return result, time.monotonic() - began

    def test_a_start_during_the_warmup_waits_for_it(self):
        self.hold_lock(as_warmup=True)
        result, took = self.start()
        self.assertIn("waiting for the boot-time warm-up", result.stderr)
        self.assertNotIn("not racing it", result.stderr)
        self.assertGreaterEqual(took, 1.0)

    def test_a_start_during_another_start_still_refuses_at_once(self):
        self.hold_lock(as_warmup=False, seconds="5")
        result, took = self.start()
        self.assertEqual(result.returncode, 5)
        self.assertIn("another start is already in its gate", result.stderr)
        self.assertLess(took, 2.0)


if __name__ == "__main__":
    unittest.main()
