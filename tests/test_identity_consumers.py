"""Consumer identity boundaries, using real CMS records and loopback TLS only."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import pwd
import ssl
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
USERSPACE = ROOT / "userspace"


class LoopbackSender(unittest.TestCase):
    """An identity on disk, and the real sender run against a loopback peer."""
    @classmethod
    def setUpClass(cls):
        cls.material = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.material.cleanup)
        cls.pairs = {}
        for name in ("disk", "self-signed"):
            cert = Path(cls.material.name) / f"{name}.pem"
            key = Path(cls.material.name) / f"{name}.key"
            subprocess.run(
                ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-x509",
                 "-days", "1", "-subj", f"/CN={name}", "-keyout", str(key),
                 "-out", str(cert)], check=True, capture_output=True)
            cls.pairs[name] = cert.read_bytes(), key.read_bytes()
        identifiers = plistlib.dumps({
            "ValidatedEmailHashes": ["1111" + "a" * 60, "2222" + "b" * 60,
                                     "3333" + "c" * 60],
            "ValidatedPhoneHashes": ["AAAA" + "d" * 60],
        })
        cls.record = subprocess.run(
            ["openssl", "cms", "-sign", "-binary", "-nodetach", "-outform", "DER",
             "-signer", str(Path(cls.material.name) / "disk.pem"),
             "-inkey", str(Path(cls.material.name) / "disk.key")],
            input=identifiers, check=True, capture_output=True).stdout

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.identity_root = self.home / ".omdrop"
        self.keys = self.identity_root / "keys"
        self.keys.mkdir(parents=True)
        for source, (cert, key) in self.pairs.items():
            suffix = ".self-signed" if source == "self-signed" else ""
            (self.keys / f"certificate{suffix}.pem").write_bytes(cert)
            (self.keys / f"key{suffix}.pem").write_bytes(key)
        (self.keys / "validation_record.cms").write_bytes(self.record)
        self.settings = self.home / ".config" / "omdrop" / "settings"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("identity_source=disk\n")
        self.runtime = self.home / "runtime"
        (self.runtime / "omdrop").mkdir(parents=True)
        self.window = self.runtime / "omdrop" / "window"
        self.env = {**os.environ, "HOME": str(self.home / "wrong-home"),
                    "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_RUNTIME_DIR": str(self.runtime),
                    "OMDROP_DEBUG": "0", "OMDROP_DEBUG_SENSITIVE": "0"}
        self.env.pop("PKEXEC_UID", None)
        self.env.pop("SUDO_UID", None)

    def write_window(self, text):
        self.window.write_text(text)
        self.window.chmod(0o600)

    def sender(self, endpoint, *options):
        # Only emulate the interface's MAC; the actual sender and resolver run,
        # and every connection goes to the explicitly supplied loopback peer.
        bootstrap = '''
import builtins, io, os, pwd, runpy, sys, types
from unittest.mock import patch
home, sender, *args = sys.argv[1:]
entry = types.SimpleNamespace(pw_dir=home, pw_uid=1000, pw_gid=os.getgid())
real_open = builtins.open
def open_file(path, *args, **kwargs):
    if str(path) == '/sys/class/net/identity-test/address':
        return io.StringIO('02:00:00:00:00:01\\n')
    return real_open(path, *args, **kwargs)
sys.path.insert(0, os.path.dirname(sender))
sys.argv = [sender, *args]
with patch('builtins.open', open_file), patch('pwd.getpwuid', return_value=entry), patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000):
    runpy.run_path(sender, run_name='__main__')
'''
        return subprocess.run(
            [sys.executable, "-c", bootstrap, str(self.home),
             str(USERSPACE / "airdrop-send.py"), "--discover-only",
             "--iface", "identity-test", "--direct", endpoint,
             "--op-timeout", "2", *options],
            env=self.env, capture_output=True, text=True, timeout=10)


class IdentityConsumerTests(LoopbackSender):
    def discover(self, *options):
        observed = {}

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                observed["certificate"] = self.connection.getpeercert(binary_form=True)
                observed["path"] = self.path
                observed["body"] = plistlib.loads(
                    self.rfile.read(int(self.headers["Content-Length"])))
                body = plistlib.dumps({"ReceiverComputerName": "Identity test peer"})
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Receiver)
        server.timeout = 5
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.keys / "certificate.pem", self.keys / "key.pem")
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata="".join(
            pair[0].decode() for pair in self.pairs.values()))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        try:
            result = self.sender(f"127.0.0.1:{server.server_port}", *options)
            worker.join(6)
        finally:
            server.server_close()
        self.assertFalse(worker.is_alive(), "loopback receiver did not finish")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "Identity test peer")
        self.assertEqual(observed["path"], "/Discover")
        return observed

    def test_sender_disk_record_matches_the_presented_tls_certificate(self):
        observed = self.discover()
        self.assertEqual(observed["certificate"], ssl.PEM_cert_to_DER_cert(
            self.pairs["disk"][0].decode()))
        self.assertEqual(observed["body"]["SenderRecordData"], self.record)

    def test_sender_explicit_keys_cannot_override_a_self_signed_window(self):
        self.write_window("source=self-signed\n")
        observed = self.discover("--keys", str(self.identity_root))
        self.assertEqual(observed["certificate"], ssl.PEM_cert_to_DER_cert(
            self.pairs["self-signed"][0].decode()))
        self.assertNotIn("SenderRecordData", observed["body"])
        self.assertEqual((self.keys / "certificate.pem").read_bytes(), self.pairs["disk"][0])
        self.assertEqual((self.keys / "validation_record.cms").read_bytes(), self.record)

    def test_sender_reports_expired_window_instead_of_using_disk(self):
        self.write_window("source=1password\nfetch_id=" + "a" * 32 + "\nhard_expiry=1\n")
        result = self.sender("127.0.0.1:1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("identity error: window-expired", result.stderr)
        self.assertNotIn("no answer", result.stderr)

    @contextlib.contextmanager
    def advertiser(self):
        # BlueZ is the hardware boundary. The real main, resolver, CMS parser,
        # payload constructor and Advertisement properties run without a radio.
        bus = MagicMock()
        manager = MagicMock()

        class ServiceObject:
            def __init__(self, bus, path):
                bus.advertisement = self

        dbus = types.ModuleType("dbus")
        service = types.ModuleType("dbus.service")
        service.Object = ServiceObject
        service.method = lambda *args, **kwargs: lambda method: method
        dbus.service = service
        dbus.mainloop = types.ModuleType("dbus.mainloop")
        dbus.mainloop.glib = types.ModuleType("dbus.mainloop.glib")
        dbus.mainloop.glib.DBusGMainLoop = MagicMock()
        dbus.SystemBus = MagicMock(return_value=bus)
        dbus.Interface = lambda *args: manager
        dbus.Dictionary = lambda value, **kwargs: value
        dbus.Array = lambda value, **kwargs: bytes(value)
        dbus.UInt16 = dbus.UInt32 = int
        dbus.Boolean = bool
        dbus.exceptions = types.SimpleNamespace(DBusException=RuntimeError)
        glib = MagicMock()
        glib.timeout_add_seconds.side_effect = lambda seconds, callback: callback()
        gi = types.ModuleType("gi")
        gi.repository = types.ModuleType("gi.repository")
        gi.repository.GLib = glib
        modules = {"dbus": dbus, "dbus.service": service,
                   "dbus.mainloop": dbus.mainloop,
                   "dbus.mainloop.glib": dbus.mainloop.glib,
                   "gi": gi, "gi.repository": gi.repository}
        entry = types.SimpleNamespace(pw_dir=str(self.home), pw_uid=1000, pw_gid=os.getgid())
        manager.RegisterAdvertisement.side_effect = (
            lambda *args, **kwargs: kwargs["reply_handler"]())
        with patch.dict(sys.modules, modules), patch.object(sys, "path", [str(USERSPACE), *sys.path]), \
                patch.dict(os.environ, self.env, clear=True), \
                patch("pwd.getpwuid", return_value=entry), \
                patch("os.getuid", return_value=1000), patch("os.geteuid", return_value=1000):
            spec = importlib.util.spec_from_file_location("identity_advertiser", USERSPACE / "ble-airdrop-adv.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield module, bus, dbus

    def advertise(self, module, bus, *options):
        with patch.object(sys, "argv", ["ble-airdrop-adv.py", "--tag", "010203", *options]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        return bus.advertisement.props["ManufacturerData"][0x004C]

    def test_advertiser_derives_packet_hashes_from_record_bytes(self):
        with self.advertiser() as (module, bus, _):
            self.assertEqual(self.advertise(module, bus), bytes.fromhex(
                "05124001020300000000031111aaaa2222333300"))

    def test_advertiser_self_signed_window_ignores_legacy_record(self):
        self.write_window("source=self-signed\n")
        with self.advertiser() as (module, bus, _):
            packet = self.advertise(module, bus)
            self.assertEqual(packet[11:19], bytes(8))

    def test_advertiser_missing_self_signed_pair_fails_before_bluez(self):
        self.settings.write_text("identity_source=self-signed\n")
        (self.keys / "certificate.self-signed.pem").unlink()
        (self.keys / "key.self-signed.pem").unlink()
        with self.advertiser() as (module, _, dbus):
            with patch.object(sys, "argv", ["ble-airdrop-adv.py"]):
                with self.assertRaisesRegex(SystemExit, "identity error: self-signed-missing"):
                    module.main()
            dbus.SystemBus.assert_not_called()
        self.assertFalse((self.keys / "certificate.self-signed.pem").exists())
        self.assertFalse((self.keys / "key.self-signed.pem").exists())

    def test_advertiser_cache_read_failure_never_advertises_zero_hashes(self):
        with self.advertiser() as (module, _, dbus):
            with patch.object(sys, "argv", ["ble-airdrop-adv.py"]), \
                    patch.object(module, "resolve_identity", side_effect=module.IdentityError("cache-unreadable-as-root")):
                with self.assertRaisesRegex(SystemExit, "identity error: cache-unreadable-as-root"):
                    module.main()
            dbus.SystemBus.assert_not_called()

    def test_explicit_research_overrides_bypass_window_selection(self):
        self.write_window("source=invalid\n")
        with self.advertiser() as (module, bus, _):
            packet = self.advertise(module, bus, "--hashes", "123456789abcdef0")
            self.assertEqual(packet[11:19], bytes.fromhex("123456789abcdef0"))
            packet = self.advertise(module, bus, "--hashes-from-record")
            self.assertEqual(packet[11:19], bytes.fromhex("1111aaaa22223333"))
            packet = self.advertise(module, bus, "--hashes-from-record", str(self.home / "absent.cms"))
            self.assertEqual(packet[11:19], bytes(8))


class SenderAnswerLimitTests(LoopbackSender):
    """A peer's /Discover answer can't make the sender buffer or walk more than
    MAX_RESPONSE_BYTES, or a plist whose shared references expand."""

    def answer(self, write):
        """Run the sender against a TLS peer whose /Discover answer `write`
        produces from its socket file. Returns the sender's result and how many
        bytes the peer managed to send before the sender hung up."""
        sent = {"bytes": 0}

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    write(self.wfile, sent)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                    pass

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Receiver)
        server.timeout = 5
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.keys / "certificate.pem", self.keys / "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        try:
            result = self.sender(f"127.0.0.1:{server.server_port}")
            worker.join(10)
        finally:
            server.server_close()
        return result, sent["bytes"]

    def test_an_endless_chunked_answer_is_cut_off_at_the_limit(self):
        chunk = b"A" * 65536

        def write(out, sent):
            out.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            for _ in range(1024):  # 64 MiB, if nobody stops it
                out.write(b"10000\r\n" + chunk + b"\r\n")
                out.flush()
                sent["bytes"] += len(chunk)

        result, sent = self.answer(write)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no answer (HTTPException)", result.stderr)
        # The limit is 4 MiB; loopback socket buffers hold a few more.
        self.assertLess(sent, 32 << 20)

    def test_an_answer_declaring_a_huge_length_is_refused_before_reading(self):
        def write(out, sent):
            out.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1073741824\r\n\r\n")
            out.write(b"A" * 65536)
            out.flush()

        result, _ = self.answer(write)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no answer (HTTPException)", result.stderr)

    def test_a_name_whose_references_expand_is_not_printed(self):
        # About 12 KB on the wire; printed whole, the name is 20 MB.
        body = plistlib.dumps({"ReceiverComputerName": ["A" * 10_000] * 2000},
                              fmt=plistlib.FMT_BINARY)

        def write(out, sent):
            out.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            out.flush()

        result, _ = self.answer(write)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("no answer (TooComplex)", result.stderr)
        self.assertLess(len(result.stdout), 1024)


if __name__ == "__main__":
    unittest.main()
