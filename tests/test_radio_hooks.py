"""The radio backend hooks: argument checks, exit codes and the contract.

The hooks in userspace/radio/ are what omdrop-core's root supervisor runs for
everything Broadcom-specific (docs/backend-contract.md in omdrop-core). They
source omdrop-broadcom.sh, the code omdrop-discoverable runs, so these tests
check the hook layer: which argv each accepts, that nothing reaches the
firmware before the checks pass, how each outcome maps to an exit code, and
that no hook writes a file core owns.

Nothing here runs as root or touches a radio. Each test copies the hooks
beside a copy of the library whose run directory, trace parameter, driver
check and root check are redirected into a temporary directory, with stub
Python helpers beside them and stub ip, dmesg, sleep, nmcli and systemctl
first on PATH.
"""
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERSPACE = ROOT / "userspace"
# The hooks that need root; `probe` is the one that must not.
HOOKS = ("up", "identity", "tick", "peer-watch", "peers", "diag", "read", "trace")
# Run-directory files core owns (backend contract, "Run directory").
CORE_FILES = ("log", "until", "reason", "rx_proven", "rx_base", "supervisor",
              "supervisor.token", "start.lock", "pids", "announce")

# brcm_iovar.py: log every call and the process group it ran in; a GET answers
# with the bytes of get.<iovar> or fails, and records its --raw path and
# whether that file already existed (raw.log); a SET fails when its value
# matches the regex in reject.<iovar>.
IOVAR = r'''import os, re, sys
stub = os.environ["STUB"]
args = sys.argv[1:]
with open(os.path.join(stub, "iovar.log"), "a") as f:
    f.write(" ".join(args) + "\n")
with open(os.path.join(stub, "iovar.pgrp"), "a") as f:
    f.write(f"{os.getpgrp()}\n")
op, name = args[2], args[3]
if op == "get":
    raw = args[args.index("--raw") + 1]
    with open(os.path.join(stub, "raw.log"), "a") as f:
        f.write(f"{raw} {os.path.isfile(raw) and not os.path.islink(raw)}\n")
    src = os.path.join(stub, "get." + name)
    if not os.path.exists(src):
        sys.exit("get failed")
    open(raw, "wb").write(open(src, "rb").read())
    sys.exit(0)
reject = os.path.join(stub, "reject." + name)
if os.path.exists(reject) and re.search(open(reject).read().strip(), args[4]):
    sys.exit("BCME_BADLEN")
'''

# A Python helper that logs its argv and exits with the code in <name>.rc,
# printing <name>.out first if it exists.
HELPER = r'''import os, sys
stub = os.environ["STUB"]
name = os.path.basename(sys.argv[0])
with open(os.path.join(stub, name + ".log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
with open(os.path.join(stub, name + ".pgrp"), "a") as f:
    f.write(f"{os.getpgrp()}\n")
out = os.path.join(stub, name + ".out")
if os.path.exists(out):
    sys.stdout.write(open(out).read())
rc = os.path.join(stub, name + ".rc")
sys.exit(int(open(rc).read()) if os.path.exists(rc) else 0)
'''

# ip: awdl0 exists while $STUB/awdl0 does, and has a link-local address while
# $STUB/linklocal does. Every `ip link set` is only logged.
IP = r'''#!/usr/bin/env bash
echo "ip $*" >> "$STUB/ip.log"
case "$*" in
  "link show awdl0")
    [[ -e $STUB/awdl0 ]] || exit 1
    echo "9: awdl0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP" ;;
  "-6 addr show dev awdl0 scope link")
    [[ -e $STUB/linklocal ]] && echo "    inet6 fe80::1/64 scope link" ;;
esac
exit 0
'''

STUBS = {
    "ip": IP,
    # -C clears; otherwise print the completions the probe should count.
    "dmesg": '#!/usr/bin/env bash\n[[ ${1:-} == -C ]] && exit 0\ncat "$STUB/dmesg" 2>/dev/null\nexit 0\n',
    "sleep": "#!/usr/bin/env bash\nexit 0\n",
    "nmcli": '#!/usr/bin/env bash\necho "nmcli $*" >> "$STUB/nmcli.log"\n'
             '[[ $1 == -g ]] && cat "$STUB/nm_state" 2>/dev/null\nexit 0\n',
    "systemctl": "#!/usr/bin/env bash\nexit 0\n",
}

# brcm_iovar.py for awdl-up's `awdl-if create`: records its pid and process
# group, then lands its write 1.5 s later unless it was stopped first.
CREATE_SLOWLY = r'''import os, sys, time
stub = os.environ["STUB"]
if "awdl-if" in sys.argv:
    with open(os.path.join(stub, "create.tmp"), "w") as f:
        f.write(f"{os.getpid()} {os.getpgrp()}\n")
    os.replace(os.path.join(stub, "create.tmp"), os.path.join(stub, "create.started"))
    time.sleep(1.5)
    open(os.path.join(stub, "create.landed"), "w").close()
sys.exit(1)
'''

PY_HELPERS = ("awdl-airdrop-adv.py", "awdl-election.py", "awdl-peer-watch.py",
              "awdl-peers.py", "awdl-stats.py", "mkpsf.py", "awdl-up")

TRANSMITTING = "".join(
    f"brcmfmac: awdl txstatus ring_ifidx=1 tx_status=0x0000 seq={i}\n" for i in range(20))


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class HookHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stub = self.root / "stub"
        self.stub.mkdir()
        self.run_dir = self.stub / "run"
        self.lib = self.root / "lib"
        (self.lib / "radio").mkdir(parents=True)
        for hook in HOOKS:
            shutil.copy2(USERSPACE / "radio" / hook, self.lib / "radio" / hook)
        # The library exactly as shipped, then the redirections a test needs.
        # Root and the driver cannot be stubbed on PATH, so they are stubbed here.
        (self.lib / "omdrop-broadcom.sh").write_text(
            (USERSPACE / "omdrop-broadcom.sh").read_text() + f'''
RUN={self.run_dir}
LOG=$RUN/log
TRACE={self.stub}/awdl_trace
need_root(){{ :; }}
driver_loaded(){{ [[ -e {self.stub}/driver ]]; }}
''')
        executable(self.lib / "brcm_iovar.py", IOVAR)
        for name in PY_HELPERS:
            executable(self.lib / name, ("#!/usr/bin/env python3\n" if name == "awdl-up" else "") + HELPER)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, text in STUBS.items():
            executable(self.bin / name, text)

    def hook(self, name, *args, hooks=None, umask=None):
        env = dict(os.environ, STUB=str(self.stub),
                   PATH=f"{self.bin}:{os.environ['PATH']}",
                   OMDROP_CONF=str(self.root / "conf"))
        # As core runs a hook: in a process group of its own. umask stands in
        # for whatever the caller of pkexec had.
        with subprocess.Popen([str((hooks or self.lib / "radio") / name), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=env, cwd="/", start_new_session=True,
                              preexec_fn=None if umask is None else (lambda: os.umask(umask))) as p:
            out, err = p.communicate(timeout=60)
        result = subprocess.CompletedProcess(p.args, p.returncode, out, err)
        result.pid = p.pid
        # Conformance: a hook never writes core's files, whatever it did.
        written = [f for f in CORE_FILES if (self.run_dir / f).exists()]
        self.assertEqual(written, [], f"{name} wrote core files: {result.stderr}")
        return result

    def log(self, name):
        path = self.stub / name
        return path.read_text() if path.exists() else ""

    def touched_firmware(self):
        return any(self.log(f) for f in ("iovar.log", "ip.log", "awdl-airdrop-adv.py.log",
                                         "awdl-peer-watch.py.log", "awdl-up.log"))


class ArgumentTests(HookHarness):
    def test_extra_arguments_are_refused_before_any_work(self):
        cases = [("up", "x"), ("identity", "x"), ("tick", "1", "x"), ("tick",),
                 ("peer-watch", "--iface", "wlan0"), ("peers", "--json"), ("diag", "5", "x"),
                 ("read", "awdl_stats", "64", "x"), ("trace", "on", "x")]
        for argv in cases:
            with self.subTest(argv=argv):
                result = self.hook(*argv)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr)
                self.assertFalse(self.touched_firmware(), result.stderr)

    def test_tick_refuses_a_sequence_that_is_not_canonical(self):
        marker = self.root / "executed"
        for value in ("", "0", "01", "-1", "1e3", "1" * 20, f"a[$(touch {marker})]"):
            with self.subTest(value=value):
                result = self.hook("tick", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse(self.touched_firmware())

    def test_diag_refuses_durations_bash_would_misread(self):
        marker = self.root / "executed"
        for value in ("601", "08", "010", "9" * 20, f"a[$(touch {marker})]", "-1"):
            with self.subTest(value=value):
                result = self.hook("diag", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("at most 600", result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse(self.touched_firmware())

    def test_read_refuses_iovars_outside_awdl(self):
        for name in ("wsec_key", "pmk", "sae_password", "awdl", "awdl_X", "../ver", ""):
            with self.subTest(name=name):
                result = self.hook("read", name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.touched_firmware())

    def test_read_refuses_lengths_out_of_range(self):
        for length in ("8193", "09", "-1", "1e3"):
            with self.subTest(length=length):
                result = self.hook("read", "awdl_stats", length)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("up to 8192", result.stderr)
                self.assertFalse(self.touched_firmware())

    def test_trace_refuses_an_unknown_mode(self):
        (self.stub / "awdl_trace").write_text("0")
        result = self.hook("trace", "maybe")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.stub / "awdl_trace").read_text(), "0")


@unittest.skipIf(os.geteuid() == 0, "checks the refusal an unprivileged caller gets")
class UnprivilegedTests(HookHarness):
    def test_every_hook_refuses_a_caller_that_is_not_root(self):
        # The shipped hooks and library, unmodified: the root check comes
        # before anything that could reach the radio.
        valid = {"read": ("awdl_stats", "512"), "diag": ("1",), "trace": ("status",), "tick": ("1",)}
        for hook in HOOKS:
            with self.subTest(hook=hook):
                result = self.hook(hook, *valid.get(hook, ()), hooks=USERSPACE / "radio")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("needs root", result.stderr)
                self.assertFalse(self.touched_firmware())


class UpTests(HookHarness):
    def interface(self, linklocal=True):
        (self.stub / "awdl0").touch()
        (self.stub / "driver").touch()
        if linklocal:
            (self.stub / "linklocal").touch()
        (self.stub / "awdl_trace").write_text("0")

    def test_no_driver_exits_4_and_says_so(self):
        result = self.hook("up")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("Wi-Fi driver is not loaded", result.stderr.splitlines()[-1])
        self.assertEqual(self.log("awdl-up.log"), "")

    def test_missing_interface_with_the_driver_loaded_is_rebuilt_once(self):
        (self.stub / "driver").touch()
        result = self.hook("up")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("could not be started", result.stderr.splitlines()[-1])
        self.assertEqual(self.log("awdl-up.log").count("\n"), 1)

    def test_a_driver_without_awdl_is_named_and_not_rebuilt(self):
        # Issue 51: the kernel's own brcmfmac is loaded, so no create can work.
        (self.stub / "driver").touch()
        say = "AirDrop's Wi-Fi driver is not built for the running kernel; rebuild it."
        executable(self.lib / "radio" / "probe", "#!/usr/bin/env bash\nprintf '%s\\n' "
                   + shlex.quote(json.dumps({"backend": "awdl", "hooks": 1, "hardware": True,
                                             "missing": [{"id": "radio", "say": say}]})))
        result = self.hook("up")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertTrue(result.stderr.splitlines()[-1].endswith(say), result.stderr)
        self.assertEqual(self.log("awdl-up.log"), "")

    def test_a_transmitting_radio_with_a_link_local_exits_0(self):
        self.interface()
        (self.stub / "dmesg").write_text(TRANSMITTING)
        result = self.hook("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("gate: probe 20 frames -> 20 completions", result.stderr)
        # Core appends each line to its log, keeping a timestamp it finds.
        for line in result.stderr.splitlines():
            self.assertRegex(line, r"^\d\d:\d\d:\d\dZ ")
        # The probe borrows awdl_trace and puts it back.
        self.assertEqual((self.stub / "awdl_trace").read_text(), "0")
        self.assertNotIn("set awdl ", self.log("iovar.log"))

    def test_a_failed_probe_exits_3(self):
        self.interface()
        (self.stub / "awdl-airdrop-adv.py.rc").write_text("1")
        result = self.hook("up")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("did not answer a test", result.stderr.splitlines()[-1])

    def test_parked_with_a_template_loaded_exits_3_without_retoggling(self):
        self.interface()
        (self.stub / "get.awdl_payload").write_bytes(b"\0" * 512)
        result = self.hook("up")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("Restarting this machine clears it", result.stderr.splitlines()[-1])
        self.assertNotIn("set awdl ", self.log("iovar.log"))

    def test_parked_without_a_template_retoggles_then_exits_3(self):
        self.interface()
        result = self.hook("up")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("second attempt usually works", result.stderr.splitlines()[-1])
        # AWDL_GATE_ENABLES is unset: three awdl=0/1 cycles.
        self.assertEqual(self.log("iovar.log").count("set awdl 00000000"), 3)

    def test_no_link_local_after_the_gate_exits_4(self):
        self.interface(linklocal=False)
        (self.stub / "dmesg").write_text(TRANSMITTING)
        result = self.hook("up")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("no link-local 15 s after the gate", result.stderr.splitlines()[-1])

    def test_a_managed_awdl0_is_released_and_named_when_it_stays_managed(self):
        self.interface(linklocal=False)
        (self.stub / "dmesg").write_text(TRANSMITTING)
        (self.stub / "nm_state").write_text("100 (connected)\n")
        result = self.hook("up")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("nmcli general reload conf", self.log("nmcli.log"))
        self.assertIn("NetworkManager is managing it", result.stderr.splitlines()[-1])


class IdentityAndTickTests(HookHarness):
    def setUp(self):
        super().setUp()
        (self.stub / "mkpsf.py.out").write_text("aabbccdd\n")

    def test_identity_falls_back_to_the_bcm4388_header_form(self):
        # BCM4388 firmware 23.20.95 rejects bare TLVs and takes
        # le16(4) + le16(4 + len) + TLVs.
        (self.stub / "reject.awdl_afs_pload").write_text("^aabb")
        result = self.hook("identity")
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = [l for l in self.log("iovar.log").splitlines() if "set awdl_afs_pload" in l]
        self.assertEqual([w.split()[-1] for w in writes], ["aabbccdd", "04000800aabbccdd"])
        self.assertIn("le16(4)+le16(4+len) header", result.stderr)

    def test_identity_exits_0_when_the_firmware_rejects_every_write(self):
        for name in ("awdl_election_metric", "awdl_payload", "awdl_afs_pload", "awdl_aftxmode"):
            (self.stub / f"reject.{name}").write_text(".")
        result = self.hook("identity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SERVICE WRITE FAILED", result.stderr)
        self.assertIn("TEMPLATE WRITE FAILED", result.stderr)
        self.assertFalse((self.run_dir / "election.state").exists())

    def test_tick_exits_1_and_records_nothing_when_the_template_is_rejected(self):
        (self.stub / "reject.awdl_payload").write_text(".")
        result = self.hook("tick", "1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.run_dir / "election.state").exists())

    def test_tick_records_an_accepted_template_then_does_nothing(self):
        first = self.hook("tick", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.run_dir / "election.state").exists())
        writes = self.log("iovar.log").count("set awdl_payload")
        again = self.hook("tick", "2")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.log("iovar.log").count("set awdl_payload"), writes)

    def test_tick_reasserts_a_link_that_lost_its_link_local(self):
        (self.stub / "awdl0").touch()
        result = self.hook("tick", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("re-asserting the link", result.stderr)
        self.assertIn("ip link set awdl0 up", self.log("ip.log"))
        (self.stub / "ip.log").unlink()
        (self.stub / "linklocal").touch()
        result = self.hook("tick", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("link set", self.log("ip.log"))


class ReadOnlyHookTests(HookHarness):
    def test_read_reaches_the_firmware_for_awdl_iovars(self):
        (self.stub / "get.awdl_peer_table").write_bytes(bytes(range(4)))
        result = self.hook("read", "awdl_peer_table", "64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("get awdl_peer_table -l 64", self.log("iovar.log"))
        self.assertEqual(result.stdout.splitlines(), ["awdl_peer_table: 4 bytes", "  00 01 02 03"])

    def test_read_without_a_reply_exits_4(self):
        for name in ("ver", "cap", "awdl_stats"):
            with self.subTest(name=name):
                result = self.hook("read", name)
                self.assertEqual(result.returncode, 4, result.stderr)
                self.assertIn(f"get {name} -l 512", self.log("iovar.log"))

    def test_peers_prints_the_table_and_exits_with_its_status(self):
        (self.stub / "get.awdl_advertisers").write_bytes(b"\1" * 16)
        (self.stub / "awdl-peers.py.out").write_text("advertisers table_len=16 peers=0\n")
        result = self.hook("peers")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "advertisers table_len=16 peers=0\n")
        self.assertEqual(list(self.run_dir.glob("advertisers.*")), [])

    def test_peers_without_a_reply_exits_4(self):
        result = self.hook("peers")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("no reply", result.stderr)

    def test_diag_runs_with_a_valid_duration(self):
        result = self.hook("diag", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("opmode      unreadable", result.stdout)
        self.assertIn("over 0s", result.stdout)

    def test_trace_switches_the_parameter(self):
        (self.stub / "awdl_trace").write_text("0")
        for mode, value in (("on", "1"), ("status", "1"), ("off", "0"), ("", "0")):
            with self.subTest(mode=mode):
                result = self.hook("trace", *([mode] if mode else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"awdl_trace={value}\n")

    def test_trace_without_the_parameter_exits_4(self):
        result = self.hook("trace", "on")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("no awdl_trace parameter", result.stderr)


class PeerWatchTests(HookHarness):
    def test_peer_watch_becomes_the_watcher(self):
        (self.stub / "awdl-peer-watch.py.rc").write_text("7")
        result = self.hook("peer-watch")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.log("awdl-peer-watch.py.log"), "--iface awdl0 --only \n")


class RunDirTests(HookHarness):
    """Root writes in the run directory only once nobody else can.

    pkexec keeps the caller's umask, so a helper without its own made the
    directory 0777 and its files 0666 after `umask 000`, and such a directory
    lasts until reboot. Every hook that writes there repairs it first.
    """

    @staticmethod
    def mode(path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_a_writable_run_directory_is_repaired_before_use(self):
        victim = self.root / "victim"
        victim.write_text("precious\n")
        self.run_dir.mkdir()
        self.run_dir.chmod(0o777)
        state = self.run_dir / "election.state"
        state.write_text("x\n")
        state.chmod(0o666)
        kept = self.run_dir / "identity.state"
        kept.write_text("y\n")
        kept.chmod(0o644)
        # Links where root used to open files by name.
        for name in ("log", "pids", "payload.bin", "read.awdl_stats.bin"):
            (self.run_dir / name).symlink_to(victim)
        (self.stub / "get.awdl_stats").write_bytes(b"\1\2")
        result = self.hook("read", "awdl_stats", "512")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("repaired", result.stderr)
        self.assertEqual(self.mode(self.run_dir), 0o755)
        # Anyone could have rewritten it, so it goes rather than being trusted.
        self.assertFalse(state.exists())
        self.assertEqual(kept.read_text(), "y\n")
        self.assertEqual([p.name for p in self.run_dir.iterdir() if p.is_symlink()], [])
        self.assertEqual(victim.read_text(), "precious\n")

    def test_a_safe_run_directory_is_left_alone(self):
        self.run_dir.mkdir()
        self.run_dir.chmod(0o755)
        state = self.run_dir / "election.state"
        state.write_text("x\n")
        state.chmod(0o644)
        result = self.hook("peers")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn("repaired", result.stderr)
        self.assertEqual(state.read_text(), "x\n")

    def test_a_link_in_place_of_the_run_directory_is_not_followed(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.run_dir.symlink_to(elsewhere)
        result = self.hook("tick", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.run_dir.is_symlink())
        self.assertTrue((self.run_dir / "election.state").exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_run_directory_that_cannot_be_made_safe_stops_the_hook(self):
        self.run_dir.write_text("not a directory\n")
        for argv in (("up",), ("identity",), ("tick", "1"), ("peer-watch",), ("peers",),
                     ("diag", "0"), ("read", "awdl_stats", "512")):
            with self.subTest(argv=argv):
                result = self.hook(*argv)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("could not be made one", result.stderr.splitlines()[-1])
                self.assertFalse(self.touched_firmware())
        self.assertEqual(self.run_dir.read_text(), "not a directory\n")

    def test_files_are_created_root_only_whatever_the_umask(self):
        result = self.hook("tick", "1", umask=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mode(self.run_dir), 0o755)
        self.assertEqual(self.mode(self.run_dir / "election.state"), 0o644)


class ReplyFileTests(HookHarness):
    """Every GET's reply goes to a file mktemp made, never to a fixed name."""

    def assert_fresh_replies(self, count):
        entries = [line.rsplit(" ", 1) for line in self.log("raw.log").splitlines()]
        self.assertEqual(len(entries), count, entries)
        paths = [path for path, _ in entries]
        self.assertEqual(len(set(paths)), len(paths), "a reply file name was reused")
        for path, existed in entries:
            # Created exclusively before brcm_iovar.py opened it, in the run
            # directory, and gone once its reader is done.
            self.assertEqual(existed, "True", path)
            self.assertEqual(Path(path).parent, self.run_dir)
            self.assertFalse(Path(path).exists(), f"{path} left behind")

    def test_read(self):
        (self.stub / "get.awdl_stats").write_bytes(b"\1\2")
        self.assertEqual(self.hook("read", "awdl_stats", "512").returncode, 0)
        self.assert_fresh_replies(1)

    def test_diag(self):
        result = self.hook("diag", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_fresh_replies(5)

    def test_tick(self):
        for seq in ("1", "2"):
            self.assertEqual(self.hook("tick", seq).returncode, 0)
        self.assert_fresh_replies(4)

    def test_identity_with_rssi_thresholds(self):
        conf = self.root / "conf"
        conf.mkdir()
        (conf / "rssi-sync-threshold").write_text("-60\n")
        (self.stub / "get.awdl_election_tree").write_bytes(bytes(42))
        result = self.hook("identity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rssi thresholds -60 dBm", result.stderr)
        # The readback with bytes 7 and 9-11 patched to -60 (0xc4).
        patched = bytearray(42)
        for off in (7, 9, 10, 11):
            patched[off] = 0xC4
        self.assertIn(f"set awdl_election_tree {patched.hex()}", self.log("iovar.log"))
        # The RSSI readback, then the election tree and opmode for the template.
        self.assert_fresh_replies(3)

    def test_up_parked_without_a_template(self):
        for name in ("awdl0", "driver", "linklocal"):
            (self.stub / name).touch()
        (self.stub / "awdl_trace").write_text("0")
        self.assertEqual(self.hook("up").returncode, 3)
        # The gate's template check, and radio_up's before it picks a sentence.
        self.assert_fresh_replies(2)


class ProcessGroupTests(HookHarness):
    """Core cancels a hook by signalling its process group; nothing may leave it."""

    def test_firmware_calls_stay_in_the_hooks_group(self):
        (self.stub / "get.awdl_stats").write_bytes(b"\1\2")
        result = self.hook("read", "awdl_stats", "512")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.log("iovar.pgrp").split()), {str(result.pid)})

    def test_rebuilding_awdl0_stays_in_the_hooks_group(self):
        (self.stub / "driver").touch()
        result = self.hook("up")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.log("awdl-up.pgrp").split(), [str(result.pid)])

    def test_every_nested_timeout_keeps_the_callers_group(self):
        scripts = [USERSPACE / "omdrop-broadcom.sh", USERSPACE / "omdrop-discoverable",
                   USERSPACE / "awdl-up", *sorted((USERSPACE / "radio").iterdir())]
        for path in scripts:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                with self.subTest(at=f"{path.name}:{n}"):
                    self.assertIsNone(re.search(r"\btimeout\s+(?!--foreground\b)[-\d]", line), line)

    def test_a_cancelled_rebuild_leaves_nothing_running(self):
        # The real awdl-up, run by the `up` hook as core runs it: only its root
        # check and the NetworkManager files it may write are redirected.
        awdl_up = (USERSPACE / "awdl-up").read_text()
        for old, new in (("[[ $EUID -eq 0 ]]", "true"),
                         ("NMCONF_VENDOR=/usr/lib/NetworkManager/conf.d/", f"NMCONF_VENDOR={self.stub}/vendor-"),
                         ("NMCONF=/etc/NetworkManager/conf.d/", f"NMCONF={self.stub}/")):
            self.assertIn(old, awdl_up)
            awdl_up = awdl_up.replace(old, new)
        executable(self.lib / "awdl-up", awdl_up)
        executable(self.lib / "brcm_iovar.py", CREATE_SLOWLY)
        (self.stub / "driver").touch()
        env = dict(os.environ, STUB=str(self.stub), PATH=f"{self.bin}:{os.environ['PATH']}",
                   OMDROP_CONF=str(self.root / "conf"), AWDL_STA="wlstub")
        hook = subprocess.Popen([str(self.lib / "radio" / "up")], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, cwd="/", start_new_session=True)
        self.addCleanup(hook.wait)
        started = self.stub / "create.started"
        deadline = time.monotonic() + 30
        while not started.exists():
            self.assertIsNone(hook.poll(), "the hook ended before awdl-up created the interface")
            self.assertLess(time.monotonic(), deadline, "awdl-up never reached the create")
            time.sleep(0.05)
        helper, group = map(int, started.read_text().split())
        self.addCleanup(self.kill_helper, helper)
        # As core cancels a hook: TERM to its process group, then KILL.
        os.killpg(hook.pid, signal.SIGTERM)
        time.sleep(0.2)
        try:
            os.killpg(hook.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        hook.wait(timeout=10)
        time.sleep(2)
        self.assertFalse((self.stub / "create.landed").exists(),
                         "the interface create landed after the hook was cancelled")
        self.assertEqual(group, hook.pid, "the firmware helper ran outside the hook's group")
        self.assertFalse(self.helper_running(helper), "the firmware helper outlived the hook")

    def helper_running(self, pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return False
        return state != "Z" and str(self.lib / "brcm_iovar.py").encode() in cmdline

    def kill_helper(self, pid):
        if self.helper_running(pid):
            os.kill(pid, signal.SIGKILL)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sys = self.root / "test-root" / "sys"
        (self.sys / "module").mkdir(parents=True)
        (self.sys / "bus" / "pci" / "devices").mkdir(parents=True)
        # As the PKGBUILD installs it.
        self.probe = self.root / "probe"
        executable(self.probe, (USERSPACE / "radio" / "probe").read_text()
                   .replace("@VERSION@", "0.9.1"))

    def pci(self, vendor, cls):
        dev = self.sys / "bus" / "pci" / "devices" / f"0000:0{len(list(self.sys.glob('bus/pci/devices/*')))}:00.0"
        dev.mkdir()
        (dev / "vendor").write_text(vendor + "\n")
        (dev / "class").write_text(cls + "\n")

    def run_probe(self, *args, probe=None):
        env = dict(os.environ, OMDROP_RADIO_TEST_ROOT=str(self.root / "test-root"),
                   PATH=f"{self.root / 'bin'}:{os.environ['PATH']}")
        return subprocess.run([str(probe or self.probe), *args], capture_output=True,
                              text=True, env=env, timeout=5, cwd="/")

    def installed(self, awdl, release=None):
        # The brcmfmac the running kernel would load, as modinfo reports it.
        (self.root / "test-root" / "lib" / "modules" / (release or os.uname().release)).mkdir(parents=True)
        parm = "awdl_create_flags:interface_create flags for AWDL (uint)\\n" if awdl else ""
        (self.root / "bin").mkdir(exist_ok=True)
        executable(self.root / "bin" / "modinfo",
                   f'#!/usr/bin/env bash\nprintf "roamoff:Do not use internal roaming engine (int)\\n{parm}"\n')

    def loaded(self, awdl):
        params = self.sys / "module" / "brcmfmac" / "parameters"
        params.mkdir(parents=True)
        (params / "roamoff").touch()
        if awdl:
            (params / "awdl_create_flags").touch()

    def answer(self):
        result = self.run_probe("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_loaded_driver_is_hardware_with_nothing_missing(self):
        self.loaded(awdl=True)
        self.assertEqual(self.answer(), {"backend": "awdl", "version": "0.9.1",
                                         "contract": "0.9.1", "hooks": 1,
                                         "hardware": True, "missing": []})

    def test_the_kernels_own_driver_loaded_in_place_of_ours_is_missing_the_radio(self):
        self.loaded(awdl=False)
        self.installed(awdl=True)
        answer = self.answer()
        self.assertEqual([m["id"] for m in answer["missing"]], ["radio"])
        self.assertIn("kernel's own", answer["missing"][0]["say"])

    def test_a_kernel_without_our_build_says_so_not_restart(self):
        # Issue 51: the DKMS build failed, so the stock brcmfmac is what loads.
        self.loaded(awdl=False)
        self.installed(awdl=False)
        say = self.answer()["missing"][0]["say"]
        self.assertIn("not built for the running kernel", say)
        self.assertIn("dkms autoinstall", say)
        self.assertNotIn("estart", say)

    def test_any_kernel_release_leaves_the_answer_valid_json(self):
        release = '7.1-custom"quote\\branch\t'
        self.installed(awdl=False, release=release)
        executable(self.root / "bin" / "uname", f"#!/usr/bin/env bash\nprintf '%s' {shlex.quote(release)}\n")
        self.loaded(awdl=False)
        self.assertIn("not built for the running kernel", self.answer()["missing"][0]["say"])

    def test_a_failed_modinfo_is_not_a_missing_build(self):
        self.loaded(awdl=False)
        self.installed(awdl=True)
        executable(self.root / "bin" / "modinfo", "#!/usr/bin/env bash\nexit 1\n")
        say = self.answer()["missing"][0]["say"]
        self.assertNotIn("not built", say)
        self.assertIn("if this message remains", say)

    def test_a_driver_with_awdl_has_nothing_missing(self):
        self.loaded(awdl=True)
        self.installed(awdl=True)
        self.assertEqual(self.answer()["missing"], [])

    def test_a_loaded_driver_with_awdl_never_asks_modinfo(self):
        # The loaded driver settles it, so a slow module read cannot delay doctor.
        self.loaded(awdl=True)
        self.installed(awdl=False)
        asked = self.root / "modinfo.asked"
        executable(self.root / "bin" / "modinfo", f"#!/usr/bin/env bash\ntouch {asked}\n")
        self.assertEqual(self.answer()["missing"], [])
        self.assertFalse(asked.exists())

    def test_a_running_kernel_without_a_module_tree_is_told_to_restart(self):
        # Upgraded without a restart: the new kernel's build is not ours to judge.
        self.pci("0x14e4", "0x028000")
        (self.root / "bin").mkdir()
        executable(self.root / "bin" / "modinfo", "#!/usr/bin/env bash\nexit 1\n")
        self.assertIn("Restart to load it", self.answer()["missing"][0]["say"])

    def test_a_broadcom_wifi_chip_without_the_driver_is_missing_the_radio(self):
        self.pci("0x106b", "0x060400")
        self.pci("0x14e4", "0x028000")
        answer = self.answer()
        self.assertTrue(answer["hardware"])
        self.assertEqual([m["id"] for m in answer["missing"]], ["radio"])
        self.assertIn("driver is not loaded", answer["missing"][0]["say"])

    def test_no_broadcom_wifi_is_no_hardware(self):
        # A Broadcom device that is not a network controller does not count.
        self.pci("0x14e4", "0x010802")
        self.pci("0x8086", "0x028000")
        answer = self.answer()
        self.assertFalse(answer["hardware"])
        self.assertEqual([m["id"] for m in answer["missing"]], ["hardware"])

    def test_anything_but_json_is_a_usage_error(self):
        for args in ((), ("--text",), ("--json", "x")):
            with self.subTest(args=args):
                result = self.run_probe(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_the_shipped_probe_answers_without_root(self):
        result = self.run_probe("--json", probe=USERSPACE / "radio" / "probe")
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual((answer["backend"], answer["hooks"]), ("awdl", 1))


def definition(source, name):
    # A function as written in the script: one line, or up to its closing brace.
    start = source.index(f"\n{name}(){{") + 1
    end = source.index("\n", start)
    if not source[start:end].rstrip().endswith("}"):
        end = source.index("\n}\n", start) + 2
    return source[start:end + 1]


class DiscoverableModeTests(HookHarness):
    """omdrop-discoverable runs the same radio_up, with its own state seams."""

    def radio_up(self):
        source = (USERSPACE / "omdrop-discoverable").read_text()
        seams = "".join(definition(source, n) for n in
                        ("rx_packets", "rx_mark", "set_reason", "rx_reset", "rx_baseline"))
        script = f'''set -uo pipefail
LIB={self.lib}
. "$LIB/omdrop-broadcom.sh"
{seams}
radio_up
'''
        env = dict(os.environ, STUB=str(self.stub), PATH=f"{self.bin}:{os.environ['PATH']}",
                   OMDROP_CONF=str(self.root / "conf"))
        self.run_dir.mkdir()
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=env, timeout=60, cwd="/")

    def test_a_failed_start_still_records_its_reason(self):
        result = self.radio_up()
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual((self.run_dir / "reason").read_text(), "no_awdl0")
        # And it still logs to the window's log, as 0.9.x always has.
        self.assertIn("Wi-Fi driver is not loaded", (self.run_dir / "log").read_text())

    def test_an_open_gate_still_takes_the_receive_baseline(self):
        for name in ("awdl0", "driver", "linklocal"):
            (self.stub / name).touch()
        (self.stub / "awdl_trace").write_text("0")
        (self.stub / "dmesg").write_text(TRANSMITTING)
        result = self.radio_up()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.run_dir / "rx_proven").read_text(), "0")
        self.assertTrue((self.run_dir / "rx_base").exists())


class PackageTests(unittest.TestCase):
    def test_package_installs_every_hook_beside_the_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "src"
            for d in ("brcmfmac", "brcmutil", "include"):
                (src / "kernel/drivers/net/wireless/broadcom/brcm80211" / d).mkdir(parents=True)
            shutil.copy2(ROOT / "dkms.conf.in", src / "dkms.conf.in")
            pkg = tmp / "pkg"
            script = f'''set -e
source {ROOT}/PKGBUILD
srcdir={src} pkgdir={pkg} startdir={ROOT}
package
echo "$pkgver"
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            pkgver = result.stdout.split()[-1]
            lib = pkg / "usr/lib/omdrop"
            self.assertTrue((lib / "omdrop-broadcom.sh").is_file())
            installed = sorted(p.name for p in (lib / "radio").iterdir())
            self.assertEqual(installed, sorted(HOOKS + ("probe",)))
            for hook in installed:
                mode = (lib / "radio" / hook).stat().st_mode
                self.assertEqual(stat.S_IMODE(mode), 0o755, hook)
            self.assertIn(f"VERSION={pkgver}\n", (lib / "radio/probe").read_text())


class DiscoverableStillUsesTheLibraryTests(unittest.TestCase):
    def test_discoverable_reaches_the_moved_read_checks(self):
        # Validation runs before the root check, so this proves the 0.9.x
        # helper sources the library and dispatches into it, without root.
        result = subprocess.run([str(USERSPACE / "omdrop-discoverable"), "read", "wsec_key"],
                                capture_output=True, text=True, timeout=30, cwd="/")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("read is limited to awdl_* iovars, ver and cap", result.stderr)

    def test_discoverable_without_its_library_refuses_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "omdrop-discoverable"
            shutil.copy2(USERSPACE / "omdrop-discoverable", helper)
            result = subprocess.run([str(helper), "status"], capture_output=True, text=True,
                                    timeout=30, cwd="/")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("cannot read", result.stderr)

    def test_discoverable_writes_root_only_files_whatever_the_umask(self):
        # pkexec passes the caller's umask on. A usage error logs to $RUN/log,
        # which is enough to see the mode the helper creates files with.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = tmp / "run"
            run.mkdir()
            run.chmod(0o755)
            helper = tmp / "omdrop-discoverable"
            shutil.copy2(USERSPACE / "omdrop-discoverable", helper)
            (tmp / "omdrop-broadcom.sh").write_text(
                (USERSPACE / "omdrop-broadcom.sh").read_text() + f"\nRUN={run}\nLOG=$RUN/log\n")
            result = subprocess.run([str(helper), "read", "wsec_key"], capture_output=True,
                                    text=True, timeout=30, cwd="/",
                                    preexec_fn=lambda: os.umask(0))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("limited to awdl_", (run / "log").read_text())
            self.assertEqual(stat.S_IMODE((run / "log").stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
