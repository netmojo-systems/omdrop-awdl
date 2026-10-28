#!/usr/bin/env bash
# Prepare an Asahi kernel tree at a tag for tests/kbuild.sh: a shallow fetch of
# the tag, the linux-asahi package's config, and `make modules_prepare`.
#
#   tests/kbuild-tree.sh <tag> <dir>
#
# The config is the asahi-alarm linux-asahi package's, pinned by commit and
# checksum. Rust and BTF are switched off: the driver uses neither, and they
# would need a matching rustc and pahole.
set -euo pipefail
tag=${1:?usage: $0 <tag> <dir>} dir=${2:?usage: $0 <tag> <dir>}

config_commit=254f8f8182582324c900df38433007a2c2e25bab
config_sha256=653ac121b5a3a34356f10cc0c4aa3286f4e9be4a5927a5267efec0f3f38c1f41

git init -q "$dir"
git -C "$dir" fetch -q --depth 1 https://github.com/AsahiLinux/linux.git "refs/tags/$tag:refs/tags/$tag"
git -C "$dir" -c advice.detachedHead=false checkout -q "$tag"

curl -fsSL -o "$dir/.config" \
  "https://raw.githubusercontent.com/asahi-alarm/PKGBUILDs/$config_commit/linux-asahi/config"
echo "$config_sha256  $dir/.config" | sha256sum -c --quiet

"$dir/scripts/config" --file "$dir/.config" -d RUST -d DEBUG_INFO_BTF
make -C "$dir" -s olddefconfig
make -C "$dir" -s -j"$(nproc)" modules_prepare
