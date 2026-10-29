# Contributing

Kernel work here is a patch series against [aurora-silicon/linux](https://github.com/aurora-silicon/linux) `aurora-wip`, the tree Omarchy's `linux-aurora` kernel builds from. It's carried in `patches/` and applied by the PKGBUILD at build time. Userspace under `userspace/` ships as ordinary files.

The main thing this project needs is **results from more Macs**, above all sending from a BCM4378 (`14e4:4425`) or BCM4388 (`14e4:4434`). Receiving is confirmed on all three chips, but sending only on the BCM4387 (`14e4:4433`). Nothing in the patches gates on a chip id, so the module builds and loads anywhere `brcmfmac` does; what differs is whether the firmware accepts an AWDL interface. Report `lspci -nn | grep -i network` with any outcome, working or not.

## What a pull request needs

The firmware's behaviour differs by chip and firmware version, and reviewers can rarely reproduce yours, so a pull request is reviewed on the evidence it includes.

### The problem

- What happened, and what you expected instead.
- How often it happens: for example, "awdl0 creation failed in 3 of 10 cycles".
- Which claims you measured and which you inferred. "The firmware drops the frame" needs a log line. Without one, say it's a guess.

### Your setup

Paste the whole output of this command, the same one the hardware-report issue form asks for:

```bash
{ { tr -d '\0' < /proc/device-tree/model && echo; } 2>/dev/null || cat /sys/class/dmi/id/product_name; lspci -nn | grep -i network; uname -r; pacman -Q brcmfmac-awdl-dkms; omdrop --version; journalctl -k -b | grep -m1 'brcmfmac.*Firmware:'; ip -br link show awdl0; } 2>&1
```

Also give each Apple device involved, with its model, OS version, and AirDrop visibility (Everyone or Contacts Only).

### Evidence from real hardware

Show the behaviour before and after the change on the hardware you describe. Paste text, not screenshots, and include the lines that show the behaviour.

| What changed | Where to get the evidence |
|---|---|
| A receive window, peers, or the data-path gate | `/run/awdl-discoverable/log` during the window, and `/usr/lib/omdrop/omdrop-discoverable status --json`, which needs no root |
| The firmware peer table | `pkexec /usr/lib/omdrop/omdrop-discoverable peers` |
| Frame delivery | `journalctl -k` with `pkexec /usr/lib/omdrop/omdrop-discoverable trace on`. `tx_status=0x0000` is an acknowledged frame; `0x0003` is a frame the firmware dropped |
| Link counters | `pkexec /usr/lib/omdrop/omdrop-discoverable diag 10` |
| The kernel module | `journalctl -k -b` from module load, including any warning or oops |

### Tests

- Run the commands under [Testing](#testing) on current `main` and on your branch. Report both results.
- Name every test that fails, and say whether it also fails on `main`.
- Add a regression test that fails without your change and passes with it. A test must not depend on the machine it runs on.

### Root helper changes

`/usr/lib/omdrop/omdrop-discoverable` runs as root, and polkit lets any user in an active local session run it without a password. Treat every argument, environment variable, and file it reads as input from an unprivileged user. For a change to the helper, explain:

- What a hostile local user can do with the new input: for example, pass a value that reaches shell arithmetic, a path, or a firmware command.
- How the input is validated before it's used.
- Which files it reads or writes as root, and whether the user controls any of those paths.

### Your branch

- Base it on current `main`, and rebase when `main` moves. Git can merge two changes to the same file without a conflict and still break both, so run the tests again after rebasing.
- Make one change per pull request.
- Author your commits as yourself. For kernel patches, follow the authorship rules below.

### Before you share logs

Logs can identify people and networks. Before posting:

- **Remove device names and contact details.** That includes names, Apple ID email addresses, and phone numbers.
- **Remove network identifiers.** The kernel log can show your Wi-Fi MAC address, and your network's name and BSSID. AWDL addresses rotate every few minutes and are fine to share.
- **Never share identity material.** That includes anything in `~/.omdrop/keys`, `validation_record.cms`, and 1Password items.

## Kernel changes: send commits, not edited patch files

`patches/*.patch` are `git format-patch` output. Editing one of those files in a GitHub PR puts **your** name on the commit in this repository, but the patch's own `From:` header is what survives into the kernel tree — and that header names the original author. Merge such a PR and your work is applied upstream under somebody else's name. That is a property of `git am`, not a slight.

So send kernel changes as commits against the kernel tree:

```sh
git clone https://github.com/aurora-silicon/linux -b aurora-wip
cd linux
git am /path/to/omdrop-awdl/patches/*.patch     # the current series
# ... your work, committed with git commit -s ...
git format-patch --no-numbered origin/aurora-wip
```

Send the resulting `.patch` files, or open a PR against this repository that adds or replaces files in `patches/` with them. Either way the `From:`, `Signed-off-by:`, and any `Co-developed-by:` trailers travel with the work, and `git am` reproduces your authorship byte for byte when the series goes upstream.

### A new patch

Yours entirely. `git commit -s` writes your `Signed-off-by:`; `format-patch` writes your `From:`. Nothing else to do.

When the series goes to `aurora-silicon/linux`, the maintainer here adds a second `Signed-off-by:` beneath yours. That is the kernel's chain of custody — every person who passes a patch along signs it — and not a claim on your work. Your `From:` is unchanged and the commit stays yours upstream.

### A change to an existing patch

Keep the original `From:` and add both trailers, which the kernel requires together:

```
Co-developed-by: Your Name <you@example.com>
Signed-off-by: Your Name <you@example.com>
```

If you have effectively rewritten the patch, change `From:` to yourself and credit the original author the same way. Say which you intend in the PR; it is a judgement call and it is easier to agree before the merge than to correct after.

### Trailers other people add

`Signed-off-by:` is the only one you add to your own work. These come from other people and are welcome on any patch, especially from anyone with hardware this has never run on:

| trailer | meaning |
| --- | --- |
| `Reviewed-by:` | I read this patch and it is sound. |
| `Tested-by:` | I ran it. Say on what chip, and what happened. |

Offer them in the pull request or the issue and they get folded into the commit message before the series goes upstream.

### Sign-off

Every commit needs `Signed-off-by:`, which asserts the [Developer Certificate of Origin](https://developercertificate.org/). `git commit -s` adds it. A patch without one cannot go upstream.

Order matters. The tree's own rule: the sign-offs "should reflect the chronological history of the patch insofar as possible", and "the last `Signed-off-by:` must always be that of the developer submitting the patch" — so yours goes first and the person carrying it upstream appends theirs.

### Kernel style

`aurora-silicon/linux` carries no contribution guide of its own; it defers to the in-tree documents, and so does this project:

- [`Documentation/process/submitting-patches.rst`](https://github.com/aurora-silicon/linux/blob/aurora-wip/Documentation/process/submitting-patches.rst)
  for commit messages, trailers, and sign-off.
- [`Documentation/process/coding-style.rst`](https://github.com/aurora-silicon/linux/blob/aurora-wip/Documentation/process/coding-style.rst)
  for the code itself.

Run the tree's own checker before sending a kernel patch, and fix what it reports about your own lines:

```sh
./scripts/checkpatch.pl --strict -g HEAD
```

### Where kernel patches end up

Two destinations, different processes, and it matters which one you are aiming at:

| destination | how |
| --- | --- |
| [aurora-silicon/linux](https://github.com/aurora-silicon/linux) `aurora-wip` — the Asahi fork Omarchy's `linux-aurora` builds from | GitHub pull request |
| mainline `brcmfmac` | email to `linux-wireless@vger.kernel.org` and the Broadcom maintainers in `MAINTAINERS`; `brcmfmac` takes no pull requests |

This project targets the fork. A patch that belongs upstream is welcome too, but send it the way upstream expects.

## Userspace changes

Ordinary pull requests against `userspace/`. Your commits keep your authorship; there is no patch-file indirection to work around.

Shell is bash with `set -euo pipefail`, Python is the standard library only — the helpers run on a machine whose network may be the thing under repair, so they do not fetch anything. Match the surrounding style: the comments explain *why*, usually with the measurement that decided it.

## Testing

```sh
tests/check-patch-drift          # do the patches still apply to a newer Asahi tag?
python3 -m unittest discover -s tests -q
tests/kbuild.sh /usr/lib/modules/$(uname -r)/build   # does the driver build, as DKMS would?
```

CI also builds the driver against the Asahi 7.1 and 7.2 tags in `.github/workflows/kbuild.yml`. To reproduce one of those builds locally:

```sh
tests/kbuild-tree.sh asahi-7.2.12-3 /tmp/linux && tests/kbuild.sh /tmp/linux
```

A hardware change also needs the setup output and evidence described in [What a pull request needs](#what-a-pull-request-needs). "Works" is not reproducible.
