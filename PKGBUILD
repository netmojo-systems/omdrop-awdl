# Maintainer: Brent Kearney <1550934+brentkearney@users.noreply.github.com>
#
# AWDL support for Apple's Broadcom Wi-Fi (BCM4378, BCM4387, BCM4388): a DKMS module, plus the root-side userspace the
# omdrop plugin drives it through.
#
# The patched brcmfmac installs to updates/dkms/, and DKMS sets the kernel's own
# brcmfmac aside only once that install succeeds, restoring it on removal. That
# is the safety story: if a kernel update breaks the build, nothing is installed
# for that kernel, its stock driver loads and Wi-Fi keeps working. You lose AWDL,
# never the network. On a kernel whose own brcmfmac already has AWDL, dkms.conf
# skips the build entirely (see dkms.conf.in).

pkgname=brcmfmac-awdl-dkms
pkgver=0.10.2
pkgrel=1
_asahitag=asahi-7.1.13-3
pkgdesc="AWDL (AirDrop link layer) support for Apple Broadcom Wi-Fi (BCM4378, BCM4387, BCM4388): DKMS module and root helpers"
arch=('aarch64')
url="https://github.com/netmojo-systems/omdrop-awdl"
license=('GPL-2.0-only')
# The helpers are bash and python3 and drive the radio through ip(8), ping(8),
# flock(1), setsid(1) and pgrep(1). polkit is what lets an unprivileged desktop
# session invoke the one helper that needs root. The sender, airdrop-send.py,
# draws its /Ask icon with Pillow and packs its /Upload with libarchive;
# zeroconf and ifaddr are its mDNS browse, used when it runs without --direct.
#
# linux-asahi-headers is a hard dependency, not an optional one. Without it the
# DKMS hook has nothing to compile against and silently builds nothing, so the
# stock brcmfmac stays loaded and awdl0 cannot be created
# (netmojo-systems/omdrop-plugin#17).
depends=('dkms' 'linux-asahi-headers' 'bash' 'python' 'iproute2' 'iputils'
         'kmod' 'polkit' 'procps-ng' 'util-linux' 'systemd' 'keyutils'
         'python-libarchive-c' 'python-pillow' 'python-zeroconf' 'python-ifaddr')
makedepends=()        # prepare() patches with patch(1), which base-devel has
optdepends=('networkmanager: keeps awdl0 unmanaged and settles the Wi-Fi MAC before awdl0 is derived from it'
            # Naming a peer wakes it with a BLE Continuity advert, so these
            # are what `send-to-peer --list --names` needs to reach a device
            # set to Contacts Only. Still optional: without them the lookup
            # says so and names whatever is already listening.
            'bluez: waking a peer to ask its name, and the opt-in advert'
            'python-dbus: waking a peer to ask its name, and the opt-in advert'
            'python-gobject: waking a peer to ask its name, and the opt-in advert')
install="${pkgname}.install"

# The three directories DKMS actually compiles, vendored pristine from Asahi's
# tree at ${_asahitag} (commit ${_asahicommit}) under kernel/. 1.4 MB.
#
# This used to be `git+https://github.com/AsahiLinux/linux.git#tag=...`, which
# makepkg fetches as a full mirror clone -- the entire kernel history, several
# GB over the network -- to read 1.4 MB of it and throw the rest away. The
# module is compiled later by DKMS against the installed kernel headers, never
# against that tree, so nothing outside these directories was ever used.
#
# Re-vendor with:
#   git archive <tag> drivers/net/wireless/broadcom/brcm80211/{brcmfmac,brcmutil,include} | tar -x -C kernel
# and re-check the patches. Keeping the kernel path prefix is what lets them
# apply with -p1 exactly as they would upstream.
# The tag is annotated, so this is the commit it points at, not the tag object.
_asahicommit=94fb23346d522edf53722357c426a3e58030beea
# makepkg's source array takes files, not directories, so the vendored tree and
# the rest of the checkout are read from ${startdir} the way they always were.
# prepare() copies the kernel sources into ${srcdir} first: patching in place
# would leave a modified tree in the checkout and make a second build fail.
source=('dkms.conf.in')
sha256sums=('SKIP')

prepare() {
  rm -rf "${srcdir}/kernel"
  cp -a "${startdir}/kernel" "${srcdir}/kernel"
  cd "${srcdir}/kernel"
  # patch(1), NOT `git apply`: this builds inside a git clone (the plugin's
  # installer clones this repo and runs makepkg in it), and `git apply`
  # resolves paths against the enclosing repository's root rather than the
  # working directory. It then reports success having changed nothing, and the
  # package ships pristine sources -- a driver with no AWDL in it.
  for p in "${startdir}"/patches/*.patch; do
    echo "applying ${p##*/}"
    patch -p1 --forward --silent -i "$p"
  done
}

package() {
  local src="${srcdir}/kernel/drivers/net/wireless/broadcom/brcm80211"
  local dest="${pkgdir}/usr/src/brcmfmac-awdl-${pkgver}"

  # Sources go at the ROOT of /usr/src/<pkg>-<ver>/, beside dkms.conf: DKMS
  # copies that directory verbatim into its own build/ and runs make with
  # M=<that dir>, so the kernel Makefile must find Makefile, brcmfmac/ and
  # brcmutil/ directly there.
  install -d "$dest"
  cp -r "$src/brcmfmac" "$src/brcmutil" "$src/include" "$dest/"
  printf 'obj-m += brcmfmac/\nobj-m += brcmutil/\n' > "$dest/Makefile"
  sed "s/@VERSION@/${pkgver}/" "${srcdir}/dkms.conf.in" > "$dest/dkms.conf"

  # The root-side userspace. It lives under /usr/lib because the polkit action
  # below binds the discoverability grant to one exact path: a helper anywhere
  # the invoking user can write to would hand that user root.
  local lib="${pkgdir}/usr/lib/omdrop"
  install -Dm755 "${startdir}/userspace/omdrop-discoverable" "${lib}/omdrop-discoverable"
  # Sourced, never executed: the Broadcom half of omdrop-discoverable (the
  # gate, the AWDL identity, the iovar helpers), shared with the hooks below.
  install -Dm644 "${startdir}/userspace/omdrop-broadcom.sh" "${lib}/omdrop-broadcom.sh"
  # The radio backend hooks omdrop-core's root supervisor runs instead of
  # omdrop-discoverable (omdrop-core's docs/backend-contract.md): one
  # executable per hook, in the root-owned directory core looks in. Each
  # sources omdrop-broadcom.sh from the directory above it.
  local hook
  for hook in up identity tick peer-watch peers diag read trace; do
    install -Dm755 "${startdir}/userspace/radio/${hook}" "${lib}/radio/${hook}"
  done
  # probe reports the package version, which omdrop-plugin compares against
  # the helper contract it needs.
  sed "s/@VERSION@/${pkgver}/" "${startdir}/userspace/radio/probe" > "${lib}/radio/probe"
  chmod 755 "${lib}/radio/probe"
  install -Dm755 "${startdir}/userspace/awdl-up" "${lib}/awdl-up"
  local helper
  for helper in awdl-airdrop-adv awdl-election awdl-mdns-respond awdl-peer-watch \
                awdl-peers awdl-stats ble-airdrop-adv brcm_iovar mkpsf \
                awdl-af-parse airdrop-send; do
    install -Dm755 "${startdir}/userspace/${helper}.py" "${lib}/${helper}.py"
  done
  # Sending. Proven 2026-09-18 to a Mac and an iPhone: dial the peer's EUI-64
  # link-local on the port it advertises, rather than waiting for an mDNS
  # advert a Mac never publishes. Neither needs root -- they read the peer
  # table through the helper above.
  install -Dm755 "${startdir}/userspace/send-to-peer" "${lib}/send-to-peer"
  install -Dm755 "${startdir}/userspace/awdl-resolve" "${lib}/awdl-resolve"
  # Imported, never executed: the machine identity and tunables every helper
  # above reads instead of carrying a baked-in MAC or hostname.
  install -Dm644 "${startdir}/userspace/awdl_identity.py" "${lib}/awdl_identity.py"
  install -Dm644 "${startdir}/userspace/awdl_debug.py" "${lib}/awdl_debug.py"
  install -Dm644 "${startdir}/userspace/awdl_plist.py" "${lib}/awdl_plist.py"

  install -Dm644 "${startdir}/packaging/org.omarchy.omdrop.policy" \
    "${pkgdir}/usr/share/polkit-1/actions/org.omarchy.omdrop.policy"
  install -Dm644 "${startdir}/packaging/awdl0.service" \
    "${pkgdir}/usr/lib/systemd/system/awdl0.service"
  # Vendor directories, not /etc: both are read after the package's own copy is
  # in place, and both leave an /etc override to the person running the machine.
  install -Dm644 "${startdir}/packaging/brcmfmac-awdl.conf" \
    "${pkgdir}/usr/lib/modprobe.d/brcmfmac-awdl.conf"
  install -Dm644 "${startdir}/packaging/99-awdl-unmanaged.conf" \
    "${pkgdir}/usr/lib/NetworkManager/conf.d/99-awdl-unmanaged.conf"

  install -Dm644 "${startdir}/README.md" "${pkgdir}/usr/share/doc/${pkgname}/README.md"
  # Documentation, deliberately not /etc/systemd/network: renaming the Wi-Fi
  # interface breaks any NetworkManager profile pinned to the old name, and
  # nothing this package installs needs the rename.
  install -Dm644 "${startdir}/packaging/10-wld0.link" \
    "${pkgdir}/usr/share/doc/${pkgname}/10-wld0.link"
}
