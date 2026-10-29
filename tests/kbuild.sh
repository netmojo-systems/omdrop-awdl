#!/usr/bin/env bash
# Build the patched driver against one kernel build tree, the way the package
# and DKMS do: the PKGBUILD's prepare() and package() staging, then dkms.conf's
# MAKE[0] with the kernel tree in place of /lib/modules/<kver>/build.
#
#   tests/kbuild.sh <kernel-build-dir>
#
# <kernel-build-dir> is a prepared tree: an installed headers package's
# /usr/lib/modules/<kver>/build, or a source tree after `make modules_prepare`.
# Fails when make fails, a module is missing, or the compiler warns about a
# file in brcmfmac/ or brcmutil/. Warnings from kernel headers are ignored.
set -euo pipefail

kdir=$(cd "${1:?usage: $0 <kernel-build-dir>}" && pwd)
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# prepare(): pristine kernel/ plus every patch, in order, with patch(1).
cp -a "$root/kernel" "$work/kernel"
for p in "$root"/patches/*.patch; do
  patch -d "$work/kernel" -p1 --forward --silent -i "$p"
done

# package(): the DKMS source root.
src=$work/kernel/drivers/net/wireless/broadcom/brcm80211
# dkms.conf's own variables and MAKE[0]. A kernel version nothing matches
# keeps the native-AWDL check from excluding the build.
kernelver=kbuild-ci-none dkms_tree=$work/dkms
# shellcheck disable=SC1091
source "$root/dkms.conf.in"
build=$dkms_tree/$PACKAGE_NAME/$PACKAGE_VERSION/build
mkdir -p "$build"
cp -r "$src/brcmfmac" "$src/brcmutil" "$src/include" "$build/"
printf 'obj-m += brcmfmac/\nobj-m += brcmutil/\n' > "$build/Makefile"

cmd=${MAKE[0]//\/lib\/modules\/${kernelver}\/build/$kdir}
[[ $cmd != "${MAKE[0]}" ]] || { echo "dkms.conf.in: MAKE[0] no longer builds in /lib/modules/\${kernelver}/build" >&2; exit 2; }

# A tree without Module.symvers (modules_prepare only) cannot resolve exports.
[[ -f $kdir/Module.symvers ]] || export KBUILD_MODPOST_WARN=1
# A clang-built kernel needs its modules built with clang.
grep -q '^CONFIG_CC_IS_CLANG=y' "$kdir/.config" && cmd+=" LLVM=1"

echo "+ $cmd"
eval "$cmd" 2>&1 | tee "$work/make.log"

for m in brcmutil/brcmutil.ko brcmfmac/brcmfmac.ko brcmfmac/wcc/brcmfmac-wcc.ko; do
  [[ -f $build/$m ]] || { echo "missing $m" >&2; exit 1; }
done
if grep -E '(^|/)(brcmfmac|brcmutil)/[^ ]+: warning:' "$work/make.log"; then
  echo "the driver builds with warnings" >&2
  exit 1
fi
echo "built: $(modinfo -F vermagic "$build/brcmfmac/brcmfmac.ko" 2>/dev/null || echo "$kdir")"
