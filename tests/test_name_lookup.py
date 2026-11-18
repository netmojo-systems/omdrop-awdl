"""A name lookup reports each answer as it arrives and asks late arrivals.

The panel's radar showed a name only when a whole lookup ended, which is the
wake's 30 s deadline whenever any peer stays silent, and a device that joined
the peer table after a lookup started waited for the next one. These pin the
two fixes against a stubbed network: no radio, no Bluetooth, no subprocesses.
"""
import contextlib
import importlib.machinery
import importlib.util
import threading
import time
import unittest
import io
import types
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_sender():
    path = ROOT / "userspace" / "send-to-peer"
    loader = importlib.machinery.SourceFileLoader("send_to_peer", str(path))
    spec = importlib.util.spec_from_loader("send_to_peer", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class NameLookupTests(unittest.TestCase):
    def setUp(self):
        self.stp = load_sender()
        self.stp.RESCAN_SECONDS = 0.05
        self.release_slow = threading.Event()
        self.addCleanup(self.release_slow.set)

        @contextlib.contextmanager
        def wake():
            yield time.monotonic() + 5
        self.stp.wake = wake

    def fake_peer_name(self, answers, slow=()):
        def peer_name(mac, port, sender_name, ready_by=None):
            if mac in slow:
                self.release_slow.wait(5)
            return answers[mac]
        self.stp.peer_name = peer_name

    def test_an_answer_is_reported_before_a_silent_peer_gives_up(self):
        self.fake_peer_name({"aa": "iMac", "bb": None}, slow={"bb"})
        seen = []

        def on_result(mac, name):
            seen.append((mac, name))
            if mac == "aa":
                # Only once the fast answer is out does the slow peer finish.
                self.release_slow.set()

        names = self.stp.peer_names(["aa", "bb"], 8770, "test", on_result=on_result)
        self.assertEqual(seen, [("aa", "iMac"), ("bb", None)])
        self.assertEqual(names, {"aa": "iMac", "bb": None})

    def test_a_peer_heard_mid_lookup_is_asked_in_the_same_lookup(self):
        self.fake_peer_name({"aa": None, "cc": "Beata's iMac"}, slow={"aa"})
        tables = iter([["aa"], ["aa", "cc"]])

        def rescan():
            table = next(tables, ["aa", "cc"])
            if "cc" in table:
                threading.Timer(0.2, self.release_slow.set).start()
            return table

        names = self.stp.peer_names(["aa"], 8770, "test", rescan=rescan)
        self.assertEqual(names.get("cc"), "Beata's iMac")

    def test_no_newcomers_are_asked_without_a_wake(self):
        self.fake_peer_name({"aa": None, "cc": "late"})
        names = self.stp.peer_names(["aa"], 8770, "test", with_wake=False,
                                    rescan=lambda: ["aa", "cc"])
        self.assertEqual(names, {"aa": None})


class SendWaitTests(unittest.TestCase):
    """A send wakes its peer from before it waits until the sender exits."""

    MAC = "7e:49:49:fe:ea:4e"

    def setUp(self):
        self.stp = load_sender()
        self.events = []
        self.stp.peers = lambda: [(self.MAC, -40)]
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

        class Ran:
            returncode = 0

        def run(cmd, **kw):
            self.events.append(("send",))
            return Ran()
        self.stp.subprocess = types.SimpleNamespace(run=run)

    def main(self, *argv, answers):
        answers = iter(answers)

        def listening(host, port, timeout=2.0):
            opened = next(answers, False)
            self.events.append(("knock", opened))
            return opened
        self.stp.listening = listening
        with unittest.mock.patch("sys.argv", ["send-to-peer", *argv, "file"]):
            with contextlib.redirect_stderr(io.StringIO()):
                return self.stp.main()

    def test_the_wake_is_up_from_before_the_wait_until_the_transfer_ends(self):
        rc = self.main("--wait", "20", answers=[False, False, True])
        self.assertEqual(rc, 0)
        self.assertEqual(self.events[0], ("wake up", 20.0 + self.stp.SEND_WAKE_SECONDS))
        self.assertEqual(self.events[-2:], [("send",), ("wake down",)])

    def test_a_peer_that_never_opens_still_stops_the_wake(self):
        self.stp.time.monotonic = iter(range(0, 10000, 5)).__next__
        rc = self.main("--wait", "20", answers=[])
        self.assertEqual(rc, 3)
        self.assertIn(("wake down",), self.events)
        self.assertNotIn(("send",), self.events)

    def test_no_wake_is_raised_with_no_wake(self):
        self.main("--wait", "20", "--no-wake", answers=[False, True])
        self.assertNotIn("wake up", [e[0] for e in self.events])



if __name__ == "__main__":
    unittest.main()
