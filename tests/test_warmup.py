"""awdl-warmup: create awdl0, run the gate once, leave awdl enabled."""

import importlib.util
import io
import os
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
gate(){
  log gate
  # The test looks at the locks while the gate runs.
  if [[ -n ${STUB_GATE_SLEEP:-} ]]; then touch "$RUN/in-gate"; sleep "$STUB_GATE_SLEEP"; fi
  return "${STUB_GATE_RC:-0}"
}
'''

# Stands in for awdl-up: records that it ran and, unless told otherwise,
# creates awdl0.
AWDL_UP = r'''#!/usr/bin/env bash
echo awdl-up >> "$STUB/calls"
[[ ${STUB_NO_AWDL0:-} ]] || touch "$STUB/awdl0"
'''


def held(lock, shared=False):
    """Whether another process holds `lock`, by trying it without waiting."""
    mode = ["-s"] if shared else []
    return subprocess.run(["flock", *mode, "-n", "-E", "75", str(lock), "true"]).returncode == 75


def wait_until(test, check, what, seconds=5):
    deadline = time.monotonic() + seconds
    while not check():
        test.assertLess(time.monotonic(), deadline, what)
        time.sleep(0.02)


class WarmupHarness(unittest.TestCase):
    """The real awdl-warmup on a stand-in library."""

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

    def warmup_env(self, **env):
        return {**os.environ, "STUB": str(self.stub), "AWDL_WARMUP_LOCK_WAIT": "1",
                "AWDL_UP_LOCK": str(self.stub / "awdl-up.lock"), **env}

    def run_warmup(self, **env):
        return subprocess.run([str(self.lib / "awdl-warmup")], capture_output=True,
                              text=True, env=self.warmup_env(**env), timeout=20, cwd="/")

    def spawn_warmup(self, **env):
        process = subprocess.Popen([str(self.lib / "awdl-warmup")], env=self.warmup_env(**env),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/")
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def calls(self):
        path = self.stub / "calls"
        return path.read_text().splitlines() if path.exists() else []


class WarmupTests(WarmupHarness):

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

    def test_warmup_lock_is_held_whenever_the_start_lock_is(self):
        # A start tells the warm-up from another start by warmup.lock.
        (self.stub / "awdl0").touch()
        run = self.stub / "run"
        warmup = self.spawn_warmup(STUB_GATE_SLEEP="1")
        wait_until(self, lambda: (run / "in-gate").exists(), "the gate never ran")
        self.assertTrue(held(run / "start.lock"))
        self.assertTrue(held(run / "warmup.lock", shared=True))
        self.assertEqual(warmup.wait(timeout=10), 0)
        self.assertFalse(held(run / "start.lock"))
        self.assertFalse(held(run / "warmup.lock"))


DISCOVERABLE = USERSPACE / "omdrop-discoverable"
BROADCOM = USERSPACE / "omdrop-broadcom.sh"


def function(source, name):
    start = source.index(f"\n{name}(){{") + 1
    return source[start:source.index("\n}\n", start) + 3]


class ProbePayloadTests(unittest.TestCase):
    """The frames the gate's probe sends, from the real helper, decoded."""

    MAC = "02:11:22:33:44:55"

    def frames(self, *options):
        spec = importlib.util.spec_from_file_location("adv", USERSPACE / "awdl-airdrop-adv.py")
        adv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adv)
        sent = []

        class Socket:
            def __init__(self, *args):
                pass

            def bind(self, address):
                pass

            def send(self, frame):
                sent.append(frame)

        fake_socket = SimpleNamespace(AF_PACKET=0, SOCK_RAW=0, AF_INET6=socket.AF_INET6,
                                      inet_pton=socket.inet_pton, inet_ntop=socket.inet_ntop,
                                      socket=Socket)
        address = lambda path, *a, **k: io.StringIO(self.MAC + "\n")
        argv = ["awdl-airdrop-adv.py", "--iface", "awdl0", "--count", "20",
                "--interval", "0.02", "--seq", "9000", *options]
        with patch.object(adv, "socket", fake_socket), \
                patch.object(adv, "time", SimpleNamespace(sleep=lambda s: None)), \
                patch.object(adv, "open", address, create=True), \
                patch.object(adv.ident, "awdl_host", return_value="secret-host-awdl"), \
                patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            adv.main()
        return sent

    def dns(self, frame):
        # 14 B of 802.3 header, 16 B of Apple SNAP, 40 B of IPv6, 8 B of UDP.
        self.assertEqual(frame[:6], bytes.fromhex("3333000000fb"))
        self.assertEqual(frame[70:74], struct.pack("!HH", 5353, 5353))
        return frame[78:]

    def test_the_warmup_probe_names_no_service_and_no_host(self):
        frames = self.frames("--neutral")
        self.assertEqual(len(frames), 20)
        for frame in frames:
            message = self.dns(frame)
            # A response with no questions or records, and nothing after it.
            self.assertEqual(message, struct.pack("!HHHHHH", 0, 0x8400, 0, 0, 0, 0))
            self.assertNotIn(b"_airdrop", frame)
            self.assertNotIn(b"secret-host", frame)

    def test_a_window_probe_is_an_airdrop_announcement(self):
        # The decoder's control: without --neutral the same frames carry the
        # four records of an AirDrop announcement.
        message = self.dns(self.frames()[0])
        self.assertEqual(struct.unpack("!HHHHHH", message[:12]), (0, 0x8400, 0, 4, 0, 0))
        self.assertIn(b"_airdrop", message)
        self.assertIn(b"secret-host-awdl", message)


class StartWaitsForWarmupTests(WarmupHarness):
    """A window opened during the boot warm-up waits for it instead of failing."""

    def setUp(self):
        super().setUp()
        (self.stub / "awdl0").touch()
        self.run = self.stub / "run"
        self.run.mkdir()
        (self.run / "until").write_text("0")
        source = DISCOVERABLE.read_text() + BROADCOM.read_text()
        self.fragments = "\n".join(function(source, n) for n in
                                   ("is_uint", "is_duration", "warmup_running", "cmd_start"))

    def other_start(self, seconds):
        # Another start: it holds the start lock and nothing else.
        holder = subprocess.Popen(["flock", str(self.run / "start.lock"), "sleep", seconds])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        wait_until(self, lambda: held(self.run / "start.lock"), "the other start never took the lock")
        return holder

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
# Past the lock, a start finds this window up and extends it.
supervisor_alive(){{ return 0; }}
{self.fragments}
cmd_start 600
'''
        began = time.monotonic()
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        return result, time.monotonic() - began

    def test_a_start_during_the_warmup_waits_for_it(self):
        self.spawn_warmup(STUB_GATE_SLEEP="1.5")
        wait_until(self, lambda: (self.run / "in-gate").exists(), "the gate never ran")
        result, took = self.start()
        self.assertIn("waiting for the boot-time warm-up", result.stderr)
        self.assertIn("window extended", result.stderr)
        self.assertNotIn("not racing it", result.stderr)
        self.assertGreaterEqual(took, 1.0)

    def test_a_start_during_another_start_still_refuses_at_once(self):
        self.other_start("8")
        result, took = self.start()
        self.assertEqual(result.returncode, 5)
        self.assertIn("another start is already in its gate", result.stderr)
        self.assertLess(took, 1.0)

    def test_a_warmup_queued_behind_another_start_makes_a_start_wait(self):
        # The warm-up holds warmup.lock while it waits for the start lock, so
        # a start then waits too rather than refusing (review 1029). Whichever
        # gets the lock after the other start, this start ends up past it.
        other = self.other_start("1")
        self.spawn_warmup(AWDL_WARMUP_LOCK_WAIT="10")
        wait_until(self, lambda: held(self.run / "warmup.lock", shared=True),
                   "the warm-up never took warmup.lock")
        result, _ = self.start()
        self.assertIn("waiting for the boot-time warm-up", result.stderr)
        self.assertIn("window extended", result.stderr)
        self.assertNotIn("not racing it", result.stderr)
        other.wait()


if __name__ == "__main__":
    unittest.main()
