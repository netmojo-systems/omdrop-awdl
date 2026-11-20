"""The firmware's channel sequence and the one we advertise come from one table."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

USERSPACE = Path(__file__).resolve().parent.parent / 'userspace'
sys.path.insert(0, str(USERSPACE))
import awdl_schedule  # noqa: E402


def run_mkpsf(conf, **extra):
    """mkpsf.chanseq() with this configuration directory, from a child process on an interface every host has."""
    env = dict(os.environ, OMDROP_CONF=conf, OMDROP_AWDL_IFACE='lo', OMDROP_INFRA_IFACE='lo',
               OMDROP_AWDL_HOST='test-host', PYTHONPATH=str(USERSPACE),
               AWDL_SCHEDULE_FILE=str(Path(conf, 'no-record')))
    env.update(extra)
    return subprocess.run([sys.executable, '-c', 'import mkpsf; print(mkpsf.chanseq().hex())'],
                          capture_output=True, text=True, env=env)


def advertised(conf, **extra):
    out = run_mkpsf(conf, **extra)
    if out.returncode:
        raise AssertionError(out.stderr)
    return bytes.fromhex(out.stdout.strip())


def channels(seq):
    """The slot channels of an encoded sequence: encoding 1 is {flags, ch}, 3 is {ch, opclass}."""
    count, enc = seq[0] + 1, seq[1]
    body = seq[6:6 + 2 * count]
    return [body[2 * k + (1 if enc == 1 else 0)] for k in range(count)]


def legacy_firmware(peer, mch, shape):
    """awdl-up's encoder before the table moved, for the four shapes it knew."""
    slots = {'sparse': [peer, 0, 149, 0, 0, 0, 0, 0, mch, 0, 149, 0, 0, 0, 0, 0],
             'dense': [peer, peer, 149, 0, 0, 0, 0, peer, mch, peer, 149, peer, 0, 0, 0, peer],
             'full': [peer, peer, 149, peer, peer, 149, peer, peer, mch, peer, 149, peer, peer, 149, peer, peer],
             'dense44': [peer, peer, 149, peer, peer, peer, peer, peer, mch, peer, 149, peer, peer, peer, peer, peer]}[shape]
    oc = {0: 0, 1: 81, 6: 81, 11: 81, 36: 128, 40: 128, 44: 128, 48: 128, 149: 128, 153: 128, 157: 128, 161: 128}
    return bytes([15, 3, 0, 3]) + (0xffff).to_bytes(2, 'little') + b''.join(bytes([c, oc[c]]) for c in slots)


class Schedule(unittest.TestCase):
    def test_the_firmware_vector_is_unchanged_for_the_shapes_awdl_up_already_wrote(self):
        for shape in ('sparse', 'dense', 'full', 'dense44'):
            for peer in (36, 44, 149):
                with self.subTest(shape=shape, peer=peer):
                    got = awdl_schedule.firmware_chanseq(awdl_schedule.slots_for(peer, 6, shape))
                    self.assertEqual(got, legacy_firmware(peer, 6, shape))

    def test_the_advertised_sequence_dwells_where_the_firmware_does_for_every_shape(self):
        for shape in awdl_schedule.SHAPES:
            for peer in (1, 11, 44, 149):
                with self.subTest(shape=shape, peer=peer), tempfile.TemporaryDirectory() as conf:
                    record = Path(conf, 'schedule')
                    record.write_text(f'{peer} 6 {shape}\n')
                    fw = awdl_schedule.firmware_chanseq(awdl_schedule.slots_for(peer, 6, shape))
                    self.assertEqual(channels(advertised(conf, AWDL_SCHEDULE_FILE=str(record))), channels(fw))

    def test_mirror_is_known_and_keeps_the_wifi_channel(self):
        self.assertEqual(awdl_schedule.slots_for(44, 6, 'mirror'),
                         [44, 0, 149, 0, 0, 149, 44, 44, 6, 0, 149, 0, 0, 149, 44, 44])
        self.assertIsNone(awdl_schedule.problem(44, 6, 'mirror', infra=44))

    def test_a_schedule_that_never_visits_the_wifi_channel_is_refused(self):
        for shape in ('dense', 'full'):
            with self.subTest(shape=shape):
                self.assertIn('never visits the Wi-Fi channel 44',
                              awdl_schedule.problem(149, 6, shape, infra=44))

    def test_the_command_refuses_before_anything_is_written(self):
        run = lambda *a: subprocess.run([sys.executable, str(USERSPACE / 'awdl_schedule.py'), *a],
                                        capture_output=True, text=True)
        self.assertEqual(run('check', '44', '6', 'mirror', '44').returncode, 0)
        bad = run('check', '149', '6', 'dense', '44')
        self.assertEqual(bad.returncode, 2)
        self.assertIn('association would drop', bad.stderr)
        self.assertEqual(run('firmware', '44', '6', 'nosuch').returncode, 2)
        self.assertEqual(run('firmware', '44', '6', 'dense').stdout.strip(),
                         legacy_firmware(44, 6, 'dense').hex())

    def test_the_wifi_band_picks_the_master_and_anything_unschedulable_keeps_6(self):
        for infra, want in ((1, 6), (6, 6), (11, 6), (36, 149), (44, 149), (149, 149), (161, 149),
                            (165, 6), (37, 6), (None, 6)):
            with self.subTest(infra=infra):
                self.assertEqual(awdl_schedule.default_master(infra), want)

    def test_only_wifi_channel_1_or_11_that_44_would_miss_takes_the_peer_slots(self):
        for infra, mch, want in ((1, 6, 1), (11, 6, 11), (1, 149, 1), (11, 1, 11),
                                 # A master on the Wi-Fi channel already visits it: the 44 slots stay.
                                 (1, 1, 44), (11, 11, 44),
                                 (6, 6, 44), (6, 149, 44), (3, 6, 44), (36, 149, 44), (44, 149, 44),
                                 (149, 149, 44), (161, 149, 44), (165, 6, 44), (None, 6, 44)):
            for shape in awdl_schedule.SHAPES:
                with self.subTest(infra=infra, mch=mch, shape=shape):
                    self.assertEqual(awdl_schedule.default_peer(infra, mch, shape), want)
        self.assertEqual(awdl_schedule.default_peer(1, 6, 'no-such-shape'), 44)

    def test_with_nothing_configured_every_shape_visits_wifi_channels_1_6_and_11_on_master_6(self):
        for infra in (1, 6, 11):
            for shape in awdl_schedule.SHAPES:
                with self.subTest(infra=infra, shape=shape):
                    mch = awdl_schedule.default_master(infra)
                    self.assertEqual(mch, 6)
                    peer = awdl_schedule.default_peer(infra, mch, shape)
                    self.assertIsNone(awdl_schedule.problem(peer, mch, shape, infra=infra))

    def test_the_peer_command_answers_for_a_master_and_shape_and_refuses_anything_else(self):
        run = lambda *a: subprocess.run([sys.executable, str(USERSPACE / 'awdl_schedule.py'), 'peer', *a],
                                        capture_output=True, text=True)
        self.assertEqual(run('6', 'dense', '1').stdout.strip(), '1')
        self.assertEqual(run('1', 'dense', '1').stdout.strip(), '44')
        self.assertEqual(run('6', 'dense').stdout.strip(), '44')
        for bad in (('x', 'dense', '1'), ('6', 'dense', 'x'), ('1',), ()):
            with self.subTest(bad=bad):
                self.assertEqual(run(*bad).returncode, 2)

    def test_the_recorded_schedule_is_advertised_whatever_the_knobs_now_say(self):
        with tempfile.TemporaryDirectory() as conf:
            for name, value in (('peer-chan', 149), ('master-chan', 6), ('chan-shape', 'dense')):
                Path(conf, name).write_text(f'{value}\n')
            record = Path(conf, 'schedule')
            record.write_text('44 149 mirror\n')
            fw = awdl_schedule.firmware_chanseq(awdl_schedule.slots_for(44, 149, 'mirror'))
            self.assertEqual(channels(advertised(conf, AWDL_SCHEDULE_FILE=str(record))), channels(fw))

    def test_an_unknown_firmware_schedule_is_not_advertised(self):
        with tempfile.TemporaryDirectory() as conf:
            for name, value in (('peer-chan', 44), ('master-chan', 6), ('chan-shape', 'mirror')):
                Path(conf, name).write_text(f'{value}\n')
            record = Path(conf, 'schedule')
            for text in (None, 'incomplete\n', '', '44 149 nosuch\n'):
                with self.subTest(record=text):
                    record.unlink(missing_ok=True)
                    if text is not None:
                        record.write_text(text)
                    out = run_mkpsf(conf, AWDL_SCHEDULE_FILE=str(record))
                    self.assertNotEqual(out.returncode, 0)
                    self.assertEqual(out.stdout, '')
                    self.assertIn('reload the driver', out.stderr)


if __name__ == '__main__':
    unittest.main()
