# omdrop-awdl

AWDL protocol activation for Apple Silicon Macs on Linux: sixteen patches to `brcmfmac`, a DKMS package that builds them, and helpers.

AWDL (Apple Wireless Direct Link) is the link layer AirDrop and AirPlay run over. These patches expose the Wi-Fi firmware's own AWDL implementation as an `awdl0` interface, instead of reimplementing the protocol in userspace.

This is the radio backend [Omdrop](https://github.com/netmojo-systems/omdrop-plugin) uses on Apple Silicon Macs running [Omarchy M](https://github.com/omacom/omarchy-mac). On PCs with other Wi-Fi cards, Omdrop 0.16.0 and later runs over [omdrop-owl](https://github.com/t4t5/omdrop-owl) instead, which implements AWDL in software. The two packages conflict, so a computer has one or the other. The functional patches are merged into `aurora-wip`, the development branch of Omarchy's `linux-aurora` kernel ([aurora-silicon/linux#24](https://github.com/aurora-silicon/linux/pull/24)). The trace fix is proposed in [#213](https://github.com/aurora-silicon/linux/pull/213). Until a `linux-aurora` release ships both, the Omdrop plugin builds these patches as a DKMS module.

- [Install](#install)
- [What the package installs](#what-the-package-installs)
- [Sending and receiving](#sending-and-receiving)
- [Configuration](#configuration)
- [Power](#power)
- [The patches](#the-patches)
- [Hardware compatibility](#hardware-compatibility)
- [Maintenance](#maintenance)
- [Provenance](#provenance)


## Install

```bash
git clone https://github.com/netmojo-systems/omdrop-awdl.git
cd omdrop-awdl
makepkg -si
```

The build is offline and takes seconds. The kernel sources DKMS compiles are vendored in `kernel/`, taken unmodified from the pinned Asahi tag (see [kernel/PROVENANCE.md](kernel/PROVENANCE.md)), and the patches are applied to a copy at build time. The package isn't on the AUR, which only indexes x86_64. The recipe proposed for Omarchy's package repository is generated into [omarchy-pkgs/](omarchy-pkgs/) from the same `PKGBUILD`.

Reboot, or reload the driver if the link can drop for a minute:

```bash
sudo modprobe -r brcmfmac_wcc brcmfmac brcmutil
sudo modprobe brcmfmac
```

The package also enables a boot-time warm-up, `awdl-warmup.service`, so the first AirDrop window after each boot opens in seconds. It takes effect from the next boot.

Without it, the first window after a boot takes about a minute: the first AWDL session on a new `awdl0` doesn't transmit until AWDL has been switched off and on, and omdrop's transmit check spends that minute finding out. The warm-up creates `awdl0` and runs that check at boot, in the background, then leaves AWDL enabled. It advertises nothing: you aren't discoverable until a window opens.

To turn it off:

```bash
sudo systemctl mask awdl-warmup.service
```

To create `awdl0` at boot without the warm-up, mask it as above and run `sudo systemctl enable awdl0.service`. If you enabled `awdl0.service` before the warm-up existed, you can leave it enabled: the two don't interfere.

If a kernel update breaks the DKMS build, the stock `brcmfmac` loads instead: DKMS sets the kernel's own driver aside only when its build installs, and puts it back when the package is removed. You lose AWDL, not the network.

On a kernel whose own `brcmfmac` already has this AWDL support, DKMS skips the build and that kernel's driver stays in use. The check looks for either of two module parameters: `awdl_create_flags`, which 0004 adds, or `awdl`, the on/off switch that replaced it upstream. With `awdl=0`, `omdrop doctor` says AirDrop is turned off in the driver.

- **`linux-aurora` 7.2.11** is such a kernel. Its `brcmfmac` has `awdl` and no `awdl_create_flags`. It also has `awdl_trace` (0009, [#213](https://github.com/aurora-silicon/linux/pull/213)), so windows prove transmit before they open, as with this package's module. Its AWDL create and destroy commands match the ones the helpers send.
- **A kernel built from `aurora-wip` as of #183** carries 0001–0008 with the same patch IDs and drives the interface the helpers use. It lacks 0009, so windows there open with transmit and receive unproven, and a radio that has stopped transmitting isn't caught before the window opens. Receive is still confirmed once data frames arrive.
- **Neither has 0010–0014, which are instrumentation:** the firmware RAM snapshot (`brcm_iovar.py awdl-if fwdump`), and the action-frame forwarding that `awdl-resolve` and `airdrop-send.py --af-log` read. Sending, receiving and naming peers don't need them.

## What the package installs

| Path | Purpose |
|---|---|
| `/usr/src/brcmfmac-awdl-<ver>/` | Patched `brcmfmac` sources, built by DKMS for every kernel |
| `/usr/lib/omdrop/omdrop-discoverable` | Opens and closes a bounded discoverability window |
| `/usr/lib/omdrop/omdrop-broadcom.sh` | The Broadcom side of `omdrop-discoverable` (data-path check, AWDL identity, diagnostics), shared with the hooks below |
| `/usr/lib/omdrop/radio/` | Radio backend hooks for omdrop-core's root helper (`probe`, `up`, `identity`, `tick`, `peer-watch`, `peers`, `diag`, `read`, `trace`); unused until omdrop-core is installed |
| `/usr/lib/omdrop/awdl-up` | Creates and configures `awdl0`; run at boot by `awdl0.service` |
| `/usr/lib/omdrop/send-to-peer` | Lists peers and sends files to one |
| `/usr/lib/omdrop/airdrop-send.py` | The sender: `/Discover`, `/Ask`, and `/Upload` over TLS on `awdl0` |
| `/usr/lib/omdrop/awdl-resolve` | Reads a peer's advertised AirDrop service from its action frames |
| `/usr/lib/omdrop/*.py` | Helpers for the tools above |
| `/usr/share/polkit-1/actions/org.omarchy.omdrop.policy` | Lets a local desktop session run `omdrop-discoverable` without a password |
| `/usr/lib/omdrop/awdl-warmup` | Creates `awdl0` and proves it transmits once; run at boot by `awdl-warmup.service` |
| `/usr/lib/systemd/system/awdl-warmup.service` | Boot-time `awdl0` setup and warm-up; enabled by the install |
| `/usr/lib/systemd/system/awdl0.service` | Boot-time `awdl0` setup without the warm-up; not enabled by the install |
| `/usr/lib/modprobe.d/brcmfmac-awdl.conf` | `debug=0x1000`, the firmware-interface bit of the debug mask; the data-path check doesn't depend on it |
| `/usr/lib/NetworkManager/conf.d/99-awdl-unmanaged.conf` | Keeps NetworkManager off `awdl0` |
| `/usr/share/doc/brcmfmac-awdl-dkms/` | This README, and an optional `10-wld0.link` the package doesn't activate |

Boot creates `awdl0` but doesn't enable AWDL. AWDL that is always on makes the radio hop channels on its slot schedule, which costs battery and Wi-Fi throughput, and leaves the machine permanently discoverable. The trade-off is that the first window takes about ten seconds to come up.

## Sending and receiving

Both directions work with Macs and iPhones, in Everyone and Contacts Only mode. Only the radio window needs root, which polkit grants without a password.

A window turns discoverability on for a set time:

```bash
pkexec /usr/lib/omdrop/omdrop-discoverable start 600   # ten minutes
pkexec /usr/lib/omdrop/omdrop-discoverable stop
/usr/lib/omdrop/omdrop-discoverable status --json      # no root needed
```

### Receiving

This package is the radio side only. The receiving HTTPS service is in the [Omdrop plugin](https://github.com/netmojo-systems/omdrop-plugin), where `omdrop on 10m` opens a window and starts the receiver together. Any AirDrop receiver listening on `[<awdl0 link-local>]:8771` works.

To send to this machine from a Contacts Only device, this machine needs an Apple-issued identity, which the sender checks it against. Without one, set the sender to **Everyone**; a self-signed certificate is rejected at TLS.

### Sending

With a window open:

```bash
/usr/lib/omdrop/send-to-peer --list               # peers in range
/usr/lib/omdrop/send-to-peer FILE                 # the only peer heard
/usr/lib/omdrop/send-to-peer --mac e2:9d:.. FILE  # choose a peer
/usr/lib/omdrop/send-to-peer --wait 120 FILE      # keep trying while an iPhone sleeps
/usr/lib/omdrop/send-to-peer FILE1 FILE2 ...      # several files, as one transfer
/usr/lib/omdrop/send-to-peer --mac hume --url https://example.com/   # a web link
```

The recipient's prompt shows the `--name` value, `Omarchy` by default. Several files go as one transfer, so the recipient accepts them once. Files must have different names, because the recipient stores each under its name alone. `--list` leaves out this machine's own AWDL address, which the firmware's tables can include.

`--url` sends a web link the way Safari on an iPhone does: the URL travels in the `/Ask` itself (`TransferType` `links`), and no `/Upload` follows. A Mac opens it in a browser tab. Only `http` and `https` links are accepted, and links and files go in separate sends.

The sender doesn't wait for mDNS. A peer's address is the EUI-64 link-local of its AWDL MAC, and its port comes from the service it advertises. A Mac with no Finder AirDrop window open publishes nothing over mDNS but still listens, so waiting for mDNS would miss it. An iPhone opens its listener only for a few seconds around other AirDrop activity, so `--wait` polls and sends the moment it opens. Opening a share sheet on the phone brings its listener up.

### Naming a peer

`send-to-peer --list --names` (or `omdrop peers -n`) asks each peer for its name with a `/Discover` request, because AirDrop advertises no names. A Contacts Only device keeps its AirDrop service shut until it recognizes a nearby sender. So during the lookup, the sender broadcasts a Bluetooth advert carrying this machine's contact hashes, as an Apple device does when its share sheet opens, and stops it when the lookup ends. `--no-wake` skips the advert, and then only devices already listening answer.

While the advert is up, the lookup re-reads the peer table every two seconds and also asks devices that arrive after it started. `--stream` prints each device's row as soon as it answers, rather than the whole listing at the end.

A send that waits for a device to start listening (`--wait`) broadcasts the same advert for as long as it waits, and stops it before the transfer starts.

The advert needs BlueZ, `python-dbus` and `python-gobject`; without them, the sender says so on stderr and reaches only devices already listening. Without an Apple-issued identity it carries zero hashes, which wake a Mac or an iPhone set to Everyone but nothing set to Contacts Only.

In the output, `(no response)` means nothing answered on the AirDrop port. `(anonymous)` means the peer answered but withheld its name, as it does when it doesn't recognize the sender. A lookup can miss a device that is awake, so if a device you expect is unnamed, run it again.

### Identity storage

The driver follows the identity selected by the Omdrop plugin for the current
receive window. With the optional 1Password integration, the plugin fetches and
validates the Apple certificate, private key, and validation record into one
Linux user-keyring entry. The driver reads that cache through `keyctl`; it never
invokes 1Password or prompts for authentication.

The plugin controls the cache lifetime and explicit lock/unlock actions.
Turning receiving off can preserve the cache for the next window. If an active
window's cached identity is missing, expired, or replaced, driver operations
report an error instead of silently using a different identity.

Local Apple identities retain `~/.omdrop/keys/certificate.pem`, `key.pem`, and
`validation_record.cms`. Self-signed identities use `certificate.self-signed.pem`
and `key.self-signed.pem` and never send a validation record. The driver never
overwrites the Apple filenames to create a fallback. A standalone sender without
a plugin window uses the local identity policy; it does not unlock or adopt a
cached 1Password identity automatically.

The cache is readable by processes with the user's keyring permissions; it is
not per-application isolation. Loading it into TLS creates userspace copies.
The sender closes temporary memory-file descriptors after loading TLS and
disables process core dumps, but cache expiry cannot erase copies held by an
already-running process. See the [shared identity contract](docs/identity-contract.md)
for the driver/plugin selection and storage rules.

### The firmware peer table holds eight entries

The firmware tracks at most eight peers, and a peer without an entry silently loses every frame sent to it. Apple devices change their AWDL MAC every few minutes, and each change takes a new entry. The peer watcher frees slots when a window starts, on `SIGTERM`, and at capacity, where it evicts the peer heard least recently. The device you're talking to stays registered, but in a busy room a quiet device can drop out and reappear. `omdrop-discoverable status` reports `peer_op=degraded` when the table is full of peers that are all audible.

### Diagnosing a transfer

```bash
pkexec /usr/lib/omdrop/omdrop-discoverable peers      # firmware peer table, with RSSI
pkexec /usr/lib/omdrop/omdrop-discoverable diag 10    # counters over ten seconds
pkexec /usr/lib/omdrop/omdrop-discoverable trace on   # per-frame tx completions in dmesg
pkexec /usr/lib/omdrop/omdrop-discoverable trace off
```

With tracing on, `tx_status=0x0000` means the peer acknowledged a frame, and `0x0003` means the firmware dropped it before transmission, usually because the peer isn't registered.

Protocol dumps are off by default. Set `OMDROP_DEBUG=1` when running a sender
(or pass `--verbose` directly to `airdrop-send.py`) to write redacted plists
under `$XDG_RUNTIME_DIR/omdrop/debug`. Validation records and certificates are
replaced by their lengths; non-plist bodies are represented only by their size.
The runtime directory must be private and owned by the invoking user.

`OMDROP_DEBUG_SENSITIVE=1` explicitly enables unredacted dumps for protocol
development. These can contain identity records and transferred data. Do not
attach them to bug reports or put them in backups.

## Configuration

Nothing is required. The helpers read the MAC, Wi-Fi interface, and hostname at runtime. To override a default, put a one-line file in `/etc/omdrop/`:

| File | Default | Meaning |
|---|---|---|
| `infra-iface` | first Broadcom Wi-Fi interface | The interface `awdl0` is derived from |
| `awdl-host` | `<short hostname>-awdl` | Name advertised over AWDL and answered over mDNS |
| `master-chan` | `6` | Master availability-window channel |
| `peer-chan` | `44` | Peer availability-window channel |
| `chan-shape` | `dense` | Slot density: `sparse`, `dense`, `full`, `dense44`, or `mirror` |
| `election-metric` | `100` | Election metric advertised and enforced |
| `rssi-sync-threshold` | firmware default | dBm floor for adopting a peer as root; `-60` keeps the election to the room |

## Wi-Fi joins and iwd

Joining a network that offers WPA2 and WPA3 through iwd keeps timing out with `status 16`, and NetworkManager reports each timeout as a missing password, so the Wi-Fi panel keeps asking for a password that is correct. NetworkManager's wpa_supplicant backend joins on the first attempt on the same machine and network. omarchy-mac makes iwd the default on these Macs ([omarchy-mac-pkgs#80](https://github.com/omacom/omarchy-mac-pkgs/issues/80)). This affects Wi-Fi, not AirDrop. To use wpa_supplicant instead:

```bash
printf '[device]\nwifi.backend=wpa_supplicant\n' | sudo tee /etc/NetworkManager/conf.d/90-wifi-backend.conf
sudo systemctl mask --now iwd.service
sudo systemctl restart NetworkManager
```

To go back, delete that file, run `sudo systemctl unmask iwd.service`, and restart NetworkManager.

## Power

An open window costs 71 mW (95% CI 59.8–82.5), measured on an M1 Pro over 5.6 hours of alternating six-minute blocks. That's 1.5% of a 4.71 W idle machine: a ten-minute window costs about 19 seconds of battery, and staying discoverable all day costs 2% of a charge. How long to stay visible is a privacy question, not a battery one. The method and data are in [issue #8](https://github.com/netmojo-systems/omdrop-awdl/issues/8#issuecomment-5731084807).

## The patches

| Patches | Purpose |
|---|---|
| 0001–0005 | Create and manage the `awdl0` interface |
| 0006 | Translate AWDL data frames at the `awdl0` boundary |
| 0007 | Tolerate txstatus for a freed flowring (fixes a NULL dereference) |
| 0008 | Bound AWDL creation, validate the data path, and set the `awdl0` MTU to 1484 |
| 0009 | Log AWDL tx completions behind the `awdl_trace` module parameter |
| 0010 | Firmware RAM snapshot vendor op |
| 0011–0013 | Log, dump and forward AWDL action frames |
| 0014 | Gate the action-frame log behind `awdl_trace` too |
| 0015 | Build against 7.1 headers as well as 7.2 (`remain_on_channel`'s `rx_addr`) |
| 0016 | Report port authorization only after the connect result (an ordering error in `asahi-7.2.12-2`) |

0001–0008 are the `brcm80211` commits [aurora-silicon/linux](https://github.com/aurora-silicon/linux) `aurora-wip` carries, with the same patch IDs: our series from [#24](https://github.com/aurora-silicon/linux/pull/24), then the maintainers' follow-up from [#183](https://github.com/aurora-silicon/linux/pull/183). On Asahi's `asahi-7.1.13-3`, applying only 0001–0008 gave a tree that differed from `aurora-wip` at `bc1823a` by one port-authorization fix in `cfg80211.c`. The tree vendored now is `asahi-7.2.12-2`. 0008 is rebased onto its reworked `brcmf_detach()`, so its patch ID no longer matches `aurora-wip`'s, though its effect is unchanged. 0015 lets that tree build against 7.1 headers as well as 7.2. 0009 restores the tx-completion log that #183 compiled out of release builds, which `omdrop-discoverable` counts to tell a transmitting radio from a parked one; it is proposed upstream in [#213](https://github.com/aurora-silicon/linux/pull/213). 0010–0014 are instrumentation that stays out of the kernel tree. The action-frame patches show the PSF and MIF frames discovery runs on. Little about this protocol is documented, so start there if you're extending this work.

## Hardware compatibility

List your Wi-Fi hardware by running `lspci -nn | grep -i network`. BCM4378 is `14e4:4425`, BCM4387 is `14e4:4433`, and BCM4388 is `14e4:4434`.

The patches configure AWDL the firmware already implements, so they only work on Apple's Broadcom Wi-Fi, not on Intel, MediaTek, or Qualcomm cards. For those, use [omdrop-owl](https://github.com/t4t5/omdrop-owl); its [Supported hardware](https://github.com/t4t5/omdrop-owl#supported-hardware) table lists the cards that work with Omdrop.

Intel-based Macs also have Broadcom Wi-Fi, but this package doesn't support them: it builds only for `aarch64`, against `linux-asahi-headers`. They are untested with either backend.

### Known to work

| Mac | Wi-Fi chip | WLAN PCI ID | Verified | Report |
| --- | --- | --- | --- | --- |
| MacBook Pro 16-inch (2021), M1 Pro | BCM4387 | `14e4:4433` | Sending and receiving, iPhone and Mac, Everyone and Contacts Only | |
| MacBook Air (2020), M1 | BCM4378 | `14e4:4425` | Receiving from an iPhone, Everyone | [omdrop-plugin#12](https://github.com/netmojo-systems/omdrop-plugin/issues/12) |
| MacBook Pro 16-inch (2023), M2 Pro | BCM4388 | `14e4:4434` | Receiving from an iPhone, Everyone | [#16](https://github.com/netmojo-systems/omdrop-awdl/issues/16) |
| MacBook Air 13-inch (2022), M2 | BCM4387 | `14e4:4433` | Receiving from a Mac | [omdrop-plugin#17](https://github.com/netmojo-systems/omdrop-plugin/issues/17) |

BCM4388 firmware `23.20.95` rejects the service record in the form BCM4387 takes, and accepts it only behind a 4-byte length header. `omdrop-discoverable` tries the BCM4387 form first and falls back to the header form ([#23](https://github.com/netmojo-systems/omdrop-awdl/pull/23)).

### Probably works

Same Wi-Fi chip as a Mac above, not yet tested:

| Mac | Apple chip | Wi-Fi chip |
| --- | --- | --- |
| MacBook Pro 14-inch (2021) | M1 Pro, M1 Max | BCM4387 |
| MacBook Pro 16-inch (2021) | M1 Max | BCM4387 |
| Mac Studio (2022) | M1 Max, M1 Ultra | BCM4387 |
| MacBook Air 15-inch (2023) | M2 | BCM4387 |
| MacBook Pro 13-inch (2020) | M1 | BCM4378 |
| Mac mini (2020) | M1 | BCM4378 |
| iMac 24-inch (2021) | M1 | BCM4378 |
| MacBook Pro 13-inch (2022) | M2 | BCM4378 |
| MacBook Pro 14-inch (2023) | M2 Pro, M2 Max | BCM4388 |
| MacBook Pro 16-inch (2023) | M2 Max | BCM4388 |
| Mac mini (2023) | M2, M2 Pro | BCM4388 |
| Mac Studio (2023) | M2 Max, M2 Ultra | BCM4388 |
| Mac Pro (2023) | M2 Ultra | BCM4388, reported; Asahi's device tree is inconsistent for this model |

Sources: the [Asahi device list](https://asahilinux.org/docs/hw/devices/device-list/), [Asahi's WLAN PCI IDs](https://github.com/AsahiLinux/linux/blob/asahi/drivers/net/wireless/broadcom/brcm80211/include/brcm_hw_ids.h), and an [Apple device-tree radio inventory](https://gist.github.com/JJTech0130/bf7dbc5b4ea1442a07bbd58bb1ae89c4).

Tried it on another Mac? [Send a hardware report](https://github.com/netmojo-systems/omdrop-awdl/issues/new?template=hardware-report.yml), working or not.

## Maintenance

The patches apply to `asahi-7.2.12-2` (commit `2bb9d16f`), the tree vendored in `kernel/`, and every file they touch is in `drivers/net/wireless/broadcom/brcm80211/brcmfmac/`. The result builds against both 7.1 and 7.2 kernel headers. The pin is deliberate, so a rebase conflict shows up at build time rather than at runtime. [kernel/PROVENANCE.md](kernel/PROVENANCE.md) has the re-vendoring steps, and `tests/check-patch-drift` checks whether the patches apply to a newer tag.

The data-path check counts per-frame `awdl txstatus` lines. The module logs them only while its `awdl_trace` parameter is set, and `omdrop-discoverable` sets it for the few seconds the check runs. The module is still built with `DEBUG`, which the firmware RAM snapshot (0010) and the debugfs files need.

Issues and pull requests are welcome. Before changing a kernel patch, read [CONTRIBUTING.md](CONTRIBUTING.md): the patches are stored as `git format-patch` files, so sending commits, not edited patch files, keeps your authorship on the commit that goes upstream.

## Provenance

The patches and helpers were developed with AI assistance. They weren't submitted to Asahi Linux, whose [generative AI policy](https://asahilinux.org/llm-policy/) forbids AI-assisted contributions; this repository makes the work available to people who want it.

The kernel patches derive from the Asahi Linux kernel and are licensed GPL-2.0-only, with authorship and `Signed-off-by` lines preserved. The userspace helpers are original work under the same licence, written from published field descriptions (Broadcom's `wlioctl.h`, the Wireshark AWDL dissector, and SEEMOO's papers) and this project's own measurements, not from [owl](https://github.com/seemoo-lab/owl)'s GPLv3 source. The sender, `airdrop-send.py`, used to import [OpenDrop](https://github.com/seemoo-lab/opendrop) (GPLv3); it now makes the same requests with its own code.

## Trademark

AirDrop is a trademark of Apple Inc. Omdrop is an independent project, not affiliated with or endorsed by Apple.

## Licence

GPL-2.0-only. See [LICENSE](LICENSE).
