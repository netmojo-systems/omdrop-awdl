# Vendored kernel sources

These three directories are copied verbatim from the Asahi Linux kernel tree. They are not this project's work, and they carry the kernel's own licence.

| | |
|---|---|
| Upstream | https://github.com/AsahiLinux/linux |
| Tag | `asahi-7.2.12-2` |
| Commit | `2bb9d16f967f11e533a851b13b148a708d59b4f3` |
| Paths | `drivers/net/wireless/broadcom/brcm80211/{brcmfmac,brcmutil,include}` |
| Licence | GPL-2.0-only, as marked in each file |

## Why they are here

DKMS compiles these three directories and nothing else. Fetching them as a git source made `makepkg` mirror-clone the entire kernel — several GB of history — to read 1.4 MB of it and discard the rest. The module is built against the installed kernel headers, never against that tree, so the rest was never used.

## Keeping them current

Nothing here is edited. The AWDL changes live in `patches/`, applied to a copy of this tree at build time, so the diff against upstream stays exactly the set of patches in that directory.

To move to a newer Asahi tag, re-extract and re-check the patches:

```
git -C <asahi-linux-checkout> archive <tag> \
  drivers/net/wireless/broadcom/brcm80211/{brcmfmac,brcmutil,include} \
  | tar -x -C kernel
for p in patches/*.patch; do (cd kernel && patch -p1 --dry-run -i "../$p"); done
```

Then update `_asahitag` and `_asahicommit` in `PKGBUILD` and this file. The kernel path prefix is preserved so the patches apply with `-p1`, exactly as they would upstream.
