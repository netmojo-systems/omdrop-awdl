"""dkms.conf stands aside on a kernel whose own brcmfmac already has AWDL.

Sourced the two ways it is in the field: by dkms, with $kernelver, and by
the pacman hook's BUILD_EXCLUSIVE_KERNEL check, with only $kver. modinfo is
a stub that reports parameters per file, so no real module is needed.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "dkms.conf.in"

# A kernel version no machine has, so /lib/modules/<kv> never exists and the
# only module the rule can find is the one placed in the fake dkms tree.
KV = "0.0.0-omdrop-test"

MODINFO = """#!/bin/bash
# modinfo -F parm FILE: the parameters recorded beside FILE, if any.
[[ $1 == -F && $2 == parm ]] || exit 1
cat "$3.parm" 2>/dev/null || exit 1
"""

NATIVE = "awdl_create_flags:interface_create flags for AWDL (uint)\ndebug:Level of debug output (int)\n"
# linux-aurora 7.2.11's brcmfmac, as modinfo -F parm prints it: the series
# after aurora-silicon/linux d4f767b and e75cc82, with no awdl_create_flags.
NATIVE_SWITCH = ("awdl_bssid:interface_create BSSID for AWDL (default: the fixed AWDL BSSID) (charp)\n"
                 "awdl:AWDL interface creation through the vendor command (default 1, 0 turns it off) (int)\n"
                 "awdl_trace:Log AWDL tx completions (int)\ndebug:Level of debug output (int)\n")
STOCK = "debug:Level of debug output (int)\np2pon:Enable legacy p2p management functionality (int)\n"


class StandAsideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "modinfo").write_text(MODINFO)
        (bindir / "modinfo").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        self.dkms_tree = self.root / "dkms"

    def archive(self, parms):
        # The kernel's own brcmfmac, as DKMS archives it when ours is installed.
        d = self.dkms_tree / "brcmfmac-awdl" / "original_module" / KV / "aarch64"
        d.mkdir(parents=True, exist_ok=True)
        (d / "brcmfmac.ko.zst").write_bytes(b"\x28\xb5\x2f\xfd")
        (d / "brcmfmac.ko.zst.parm").write_text(parms)
        (d / "brcmfmac.ko.zst.origin").write_text("/lib/modules/x/kernel/brcmfmac.ko.zst\n")

    def source(self, assignment):
        # Each reader's view of BUILD_EXCLUSIVE_KERNEL, plus everything the file
        # printed: the hook takes the file's stdout as part of the value.
        script = f'''
{assignment}
dkms_tree={self.dkms_tree}
value=$(source {CONF}; printf '%s' "${{BUILD_EXCLUSIVE_KERNEL-UNSET}}")
printf '%s' "$value"
'''
        result = subprocess.run(["bash", "-c", script], env=self.env,
                                capture_output=True, text=True, check=True)
        return result.stdout

    def builds(self, value, kernel=KV):
        # The hook's own test: build when the kernel version matches the regex.
        result = subprocess.run(["bash", "-c", '[[ "$1" =~ $2 ]]', "_", kernel,
                                 "" if value == "UNSET" else value])
        return result.returncode == 0

    def test_a_kernel_with_native_awdl_is_skipped_by_dkms(self):
        self.archive(NATIVE)
        value = self.source(f"kernelver={KV}")
        self.assertEqual(value, "^$")
        self.assertFalse(self.builds(value))

    def test_a_kernel_with_native_awdl_is_skipped_by_the_pacman_hook(self):
        self.archive(NATIVE)
        value = self.source(f"kver={KV}")
        self.assertEqual(value, "^$")
        self.assertFalse(self.builds(value))

    def test_a_kernel_with_the_awdl_switch_is_skipped(self):
        self.archive(NATIVE_SWITCH)
        for assignment in (f"kernelver={KV}", f"kver={KV}"):
            with self.subTest(assignment=assignment):
                self.assertEqual(self.source(assignment), "^$")

    def test_other_awdl_parameters_alone_are_not_native(self):
        # Only the two markers count: a prefix match on "awdl" would not do.
        self.archive(STOCK + "awdl_trace:Log AWDL tx completions (int)\n")
        self.assertEqual(self.source(f"kernelver={KV}"), "UNSET")

    def test_a_stock_kernel_still_builds(self):
        self.archive(STOCK)
        for assignment in (f"kernelver={KV}", f"kver={KV}"):
            with self.subTest(assignment=assignment):
                value = self.source(assignment)
                self.assertEqual(value, "UNSET")
                self.assertTrue(self.builds(value))

    def test_an_unreadable_module_still_builds(self):
        # A module modinfo cannot read is no evidence of native support.
        d = self.dkms_tree / "brcmfmac-awdl" / "original_module" / KV / "aarch64"
        d.mkdir(parents=True)
        (d / "brcmfmac.ko.zst").write_bytes(b"")
        self.assertEqual(self.source(f"kernelver={KV}"), "UNSET")

    def test_no_kernel_version_changes_nothing(self):
        self.archive(NATIVE)
        self.assertEqual(self.source(""), "UNSET")


if __name__ == "__main__":
    unittest.main()
