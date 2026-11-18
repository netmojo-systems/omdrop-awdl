"""send-to-peer dials the AirDrop port a peer announced, and keeps the wake up
through a send to an Android phone.

Android's Quick Share serves AirDrop on a random port, not 8770, and closes the
receiver about ten seconds after our BLE wake stops. The helper's `peers`
reports the port as a trailing `port=N`; a helper that prints none (this
repository's) leaves every peer on --port. A stubbed network, subprocesses and
Bluetooth: no radio, no phone.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import time
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANDROID = "aa:50:8e:d9:c9:ce"   # announced port 35471, in the Galaxy S24 run that worked
APPLE = "7e:49:49:fe:ea:4e"


def load_sender():
    path = ROOT / "userspace" / "send-to-peer"
    loader = importlib.machinery.SourceFileLoader("send_to_peer", str(path))
    spec = importlib.util.spec_from_loader("send_to_peer", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AnnouncedPortTests(unittest.TestCase):
    def setUp(self):
        self.stp = load_sender()
        self.events = []
        self.stp.PORTS = {ANDROID: 35471}
        self.stp.peers = lambda: [(ANDROID, None), (APPLE, None)]
        # The module's own clock and subprocess, so nothing leaks into the
        # real modules other tests use.
        self.stp.time = types.SimpleNamespace(
            monotonic=time.monotonic, sleep=lambda s: None,
            time=time.time, strftime=time.strftime, gmtime=time.gmtime)

        @contextlib.contextmanager
        def wake(seconds=30, purpose=""):
            self.events.append(("wake up", seconds))
            try:
                yield time.monotonic() + seconds
            finally:
                self.events.append(("wake down",))
        self.stp.wake = wake

        def run(cmd, **kw):
            self.events.append(("send",))
            return types.SimpleNamespace(returncode=0)
        self.stp.subprocess = types.SimpleNamespace(run=run)

    def main(self, *argv, listening=(True,)):
        answers = iter(listening)

        def knock(host, port, timeout=2.0):
            self.events.append(("knock", port))
            return next(answers, False)
        self.stp.listening = knock
        out = io.StringIO()
        with unittest.mock.patch("sys.argv", ["send-to-peer", *argv]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = self.stp.main()
        return rc, out.getvalue()

    def names(self):
        return [e[0] for e in self.events]

    def test_the_table_read_keeps_a_port_while_its_peer_stays_and_drops_it_with_the_peer(self):
        stp = load_sender()
        out = types.SimpleNamespace(
            returncode=0, stdout=f"{ANDROID} rssi=? port=35471\n{APPLE} rssi=?\n", stderr="")
        stp.subprocess = types.SimpleNamespace(run=lambda *a, **k: out)
        registered = [ANDROID, APPLE]
        stp.registered_peers = lambda: registered
        stp.own_address = lambda: None
        stp.peers()
        self.assertEqual(stp.PORTS, {ANDROID: 35471})
        out.stdout = f"{ANDROID} rssi=?\n"   # a read that misses the port line
        stp.peers()
        self.assertEqual(stp.PORTS, {ANDROID: 35471})
        out.stdout = ""                      # the phone has left
        registered[:] = [APPLE]
        stp.peers()
        self.assertEqual(stp.PORTS, {})

    def test_a_send_dials_the_announced_port_and_apple_gets_8770(self):
        _, android = self.main("--mac", ANDROID, "--print", "f")
        _, apple = self.main("--mac", APPLE, "--print", "f")
        self.assertIn("%awdl0]:35471", android)
        self.assertIn("%awdl0]:8770", apple)

    def test_the_listing_shows_each_peers_port(self):
        _, out = self.main("--list")
        endpoint = {line.split()[0]: line.split()[-1] for line in out.splitlines()}
        self.assertTrue(endpoint[ANDROID].endswith("%awdl0]:35471"))
        self.assertTrue(endpoint[APPLE].endswith("%awdl0]:8770"))

    def test_a_knock_follows_a_port_announced_while_it_waits(self):
        def knock(host, port, timeout=2.0):
            self.events.append(("knock", port))
            self.stp.PORTS = {ANDROID: 35471}   # the table is re-read meanwhile
            return port == 35471
        self.stp.PORTS = {}
        self.stp.listening = knock
        self.assertTrue(self.stp._knock(ANDROID, 8770, time.monotonic() + 5))
        self.assertEqual([e[1] for e in self.events], [8770, 35471])

    def test_a_names_listing_wakes_an_empty_table_and_lists_who_joins(self):
        # An S24 already in Quick Share receive mode joins AWDL only after it
        # hears the wake, so the table is empty when the lookup starts.
        tables = iter([[], [], [(ANDROID, None)]])
        self.stp.peers = lambda: next(tables, [(ANDROID, None)])
        self.stp.peer_name = lambda mac, port, name, ready_by=None: "Kerem S24 Ultra"

        @contextlib.contextmanager
        def wake(seconds=30, purpose=""):
            self.events.append(("wake up", seconds))
            yield time.monotonic() + 0.3
            self.events.append(("wake down",))
        self.stp.wake = wake
        self.stp.RESCAN_SECONDS = 0.05
        rc, out = self.main("--list", "-n", "--stream")
        self.assertEqual(rc, 0)
        self.assertEqual(self.names()[0], "wake up")
        self.assertIn(ANDROID, out)
        self.assertIn("Kerem S24 Ultra", out)

    def test_a_names_listing_whose_peers_all_answer_returns_before_the_wake_ends(self):
        reads = []
        self.stp.peers = lambda: reads.append(1) or [(ANDROID, None)]
        self.stp.peer_name = lambda mac, port, name, ready_by=None: "Kerem S24 Ultra"

        @contextlib.contextmanager
        def wake(seconds=30, purpose=""):
            yield time.monotonic() + 3
        self.stp.wake = wake
        self.stp.RESCAN_SECONDS = 0.05
        t0 = time.monotonic()
        rc, out = self.main("--list", "-n")
        self.assertEqual(rc, 0)
        self.assertIn("Kerem S24 Ultra", out)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertLessEqual(len(reads), 2)

    def test_a_names_listing_nobody_joins_says_so_and_fails(self):
        self.stp.peers = lambda: []

        @contextlib.contextmanager
        def wake(seconds=30, purpose=""):
            yield time.monotonic() + 0.2
        self.stp.wake = wake
        self.stp.RESCAN_SECONDS = 0.05
        rc, out = self.main("--list", "-n", "--stream")
        self.assertEqual(rc, 1)
        self.assertIn("no devices found", out)

    def test_a_send_by_name_follows_a_port_announced_after_the_first_read(self):
        # The first two reads have the phone but not its port.
        self.stp.PORTS = {}
        reads = iter([{}, {}])

        def peers():
            self.stp.PORTS = next(reads, {ANDROID: 35471})
            return [(ANDROID, None)]
        self.stp.peers = peers
        self.stp.RESCAN_SECONDS = 0.05

        # Like _knock: the phone answers only once PORTS holds its port.
        def ask(mac, port, name, ready_by=None):
            while time.monotonic() < ready_by:
                if self.stp.PORTS.get(mac) == 35471:
                    return "Kerem S24 Ultra"
                time.sleep(0.01)
            return None
        self.stp.peer_name = ask

        @contextlib.contextmanager
        def wake(seconds=30, purpose=""):
            yield time.monotonic() + 1
        self.stp.wake = wake
        rc, out = self.main("--mac", "S24", "--print", "f")
        self.assertEqual(rc, 0)
        self.assertIn("%awdl0]:35471", out)

    def test_an_empty_table_without_names_still_says_nothing_is_there(self):
        self.stp.peers = lambda: []
        rc, _ = self.main("--list")
        self.assertEqual(rc, 1)
        self.assertNotIn("wake up", self.names())

    def test_android_keeps_the_wake_up_from_before_the_send_until_it_ends(self):
        rc, _ = self.main("--mac", ANDROID, "f", "--wait", "30")   # receiver already open
        self.assertEqual(rc, 0)
        self.assertEqual(self.events, [("wake up", 30 + self.stp.SEND_WAKE_SECONDS),
                                       ("knock", 35471), ("send",), ("wake down",)])

    def test_android_gets_one_wake_for_the_wait_and_the_send(self):
        rc, _ = self.main("--mac", ANDROID, "f", "--wait", "30", listening=(False, False, True))
        self.assertEqual(rc, 0)
        self.assertEqual(self.names().count("wake up"), 1)
        self.assertLess(self.names().index("wake up"), self.names().index("knock"))
        self.assertGreater(self.names().index("wake down"), self.names().index("send"))

    def test_a_port_announced_during_the_wait_is_dialled_under_the_one_send_wake(self):
        # The phone is in the table before its port line: the first read has
        # no port, a later one does.
        self.stp.PORTS = {}
        reads = iter([{}, {ANDROID: 35471}])

        def peers():
            self.stp.PORTS = next(reads, {ANDROID: 35471})
            return [(ANDROID, None)]
        self.stp.peers = peers
        self.stp.RESCAN_SECONDS = 0
        sent = []
        self.stp.subprocess = types.SimpleNamespace(
            run=lambda cmd, **kw: sent.append(cmd) or self.events.append(("send",))
            or types.SimpleNamespace(returncode=0))
        rc, _ = self.main("--mac", ANDROID, "f", "--wait", "30",
                          listening=(False, False, False, True))
        self.assertEqual(rc, 0)
        self.assertTrue(sent[0][3].endswith("%awdl0]:35471"))
        self.assertEqual([e for e in self.events if e[0] == "knock"][-1], ("knock", 35471))
        # One wake covers the wait and the send; it drops only after the sender has run.
        self.assertEqual([e for e in self.events if e[0] == "wake up"],
                         [("wake up", 30 + self.stp.SEND_WAKE_SECONDS)])
        self.assertEqual(self.events[-2:], [("send",), ("wake down",)])

    def test_android_without_wake_raises_none(self):
        self.assertEqual(self.main("--mac", ANDROID, "f", "--no-wake")[0], 0)
        self.assertNotIn("wake up", self.names())

    def test_an_apple_peer_keeps_the_wake_up_from_before_the_first_knock_until_the_send_ends(self):
        for listening in ((True,), (False, True)):
            with self.subTest(listening=listening):
                self.events.clear()
                self.assertEqual(self.main("--mac", APPLE, "f", "--wait", "30", listening=listening)[0], 0)
                self.assertEqual(self.events[0], ("wake up", 30 + self.stp.SEND_WAKE_SECONDS))
                self.assertEqual(self.events[1], ("knock", 8770))
                self.assertEqual(self.events[-2:], [("send",), ("wake down",)])
                self.assertEqual(self.names().count("wake up"), 1)


if __name__ == "__main__":
    unittest.main()
