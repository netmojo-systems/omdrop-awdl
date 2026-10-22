"""A peer's binary plist can't make the decoder build more than the limits."""
import plistlib
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "userspace"))
import awdl_plist  # noqa: E402


def many_ids(members=200_000, ids=1000):
    """One array of `members` references, given `ids` object IDs, and a root
    array naming every one of those IDs: about 400 KB."""
    count = ids + 3
    body = bytearray(b"bplist00")
    offsets = [len(body)]
    body += b"\x10\x01"                      # object 0: the integer 1
    array_at = len(body)
    body += b"\xaf\x12" + struct.pack(">I", members) + struct.pack(">H", 0) * members
    offsets += [array_at] * (ids + 1)        # objects 1..ids+1: that array
    offsets.append(len(body))
    body += b"\xaf\x12" + struct.pack(">I", ids)
    body += b"".join(struct.pack(">H", i) for i in range(2, ids + 2))
    table = len(body)
    body += b"".join(struct.pack(">I", o) for o in offsets)
    body += bytes(6) + bytes([4, 2]) + struct.pack(">QQQ", count, count - 1, table)
    return bytes(body)


class BoundedDecodeTests(unittest.TestCase):
    def test_an_ordinary_answer_decodes(self):
        answer = {"ReceiverComputerName": "Mac", "ReceiverRecordData": b"\x01" * 3718}
        self.assertEqual(awdl_plist.loads(plistlib.dumps(answer, fmt=plistlib.FMT_BINARY)), answer)
        self.assertEqual(awdl_plist.loads(plistlib.dumps(answer)), answer)

    def test_one_array_under_many_ids_is_refused_while_decoding(self):
        # plistlib builds the array once per ID: 1000 lists, 1.6 GB.
        bomb = many_ids()
        self.assertLess(len(bomb), 1 << 20)
        started = time.monotonic()
        with self.assertRaises(awdl_plist.TooComplex):
            awdl_plist.loads(bomb)
        self.assertLess(time.monotonic() - started, 2)
        # The same shape with two IDs is a valid plist within the limits.
        self.assertEqual(len(awdl_plist.loads(many_ids(members=1000, ids=2))), 2)

    def test_a_count_larger_than_the_input_is_refused(self):
        # An array claiming 2**40 members, in a 50-byte file.
        body = bytearray(b"bplist00")
        body += b"\xaf\x13" + struct.pack(">Q", 1 << 40)
        table = len(body)
        body += bytes([8]) + bytes(6) + bytes([1, 1]) + struct.pack(">QQQ", 1, 0, table)
        with self.assertRaises(plistlib.InvalidFileException):
            awdl_plist.loads(bytes(body))

    def test_shared_references_and_cycles_are_refused(self):
        with self.assertRaises(awdl_plist.TooComplex):
            awdl_plist.loads(plistlib.dumps(["A" * 10_000] * 2000, fmt=plistlib.FMT_BINARY))
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(awdl_plist.TooComplex):
            awdl_plist.loads(plistlib.dumps(cycle, fmt=plistlib.FMT_BINARY))


if __name__ == "__main__":
    unittest.main()
