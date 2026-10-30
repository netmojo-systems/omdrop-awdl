# shellcheck shell=bash
# The Broadcom half of omdrop-discoverable, shared with the radio backend
# hooks in radio/ (docs: omdrop-core's docs/backend-contract.md). Sourced,
# never executed: omdrop-discoverable sources it, and so does every hook.
#
# Everything here was moved out of omdrop-discoverable unchanged, so the 0.9.x
# helper and the hooks run the same code: the data-path gate, the AWDL
# identity writes, the election tracking, the iovar helpers and the read-only
# diagnostics, plus the constants, logging and number checks they all use.
#
# The caller sets LIB, the directory holding the Python helpers
# (/usr/lib/omdrop when packaged), before sourcing this file, and runs as
# `set -uo pipefail`.

# Per-machine tunables and the diagnostic switches. Absent is the normal case:
# every knob below has a working default, and the directory only exists on a
# machine someone has tuned.
CONF=${OMDROP_CONF:-/etc/omdrop}
export OMDROP_CONF="$CONF"
IF=awdl0
RUN=/run/awdl-discoverable
LOG=$RUN/log
SUPERVISOR_UID=0
# How many awdl=0/1 cycles to spend fighting a parked data path. Each costs
# ~40 s. The park is ~50% per enable and streaky, so 3 is the point where the
# expected failure rate (~1 in 8) stops improving faster than the user's
# patience degrades.
ENABLES=${AWDL_GATE_ENABLES:-3}
# How long a start waits for the boot-time warm-up's gate (awdl-warmup).
WARMUP_WAIT=${AWDL_WARMUP_WAIT:-180}
# How long a start that finds the start lock held looks for the warm-up before
# taking the holder for another start. The warm-up writes $RUN/warmup just
# after it takes the lock and removes it just before it lets go, so one look
# can miss it.
WARMUP_HANDOFF=2

# stderr always; the file only when /run/awdl-discoverable exists and is ours
# (status runs as the user, and a noisy tee failure is not a status result).
log(){ local line; line="$(date -u +%H:%M:%SZ) $*"
       printf '%s\n' "$line" >&2
       [[ -d $RUN && -w $RUN ]] && printf '%s\n' "$line" >> "$LOG" 2>/dev/null
       return 0; }
die(){ local rc=$1; shift; log "$*"; exit "$rc"; }
now(){ date +%s; }

need_root(){ [[ $EUID -eq 0 ]] || die 2 "needs root: pkexec $0 $*"; }

# run_dir: make $RUN a directory only root can change, before root writes in
# it. pkexec keeps the caller's umask, and a helper without its own `umask 022`
# run after `umask 000` created this directory 0777 and its files 0666. Anyone
# could then plant `pids` (root signals every pid it names) or a link where
# root opens a file by name (security review, RADIO-RUNDIR-TRUST). /run is a
# tmpfs, so such a directory lasts until reboot, across a package upgrade.
# Repaired here: never through a link, group and other write taken from the
# directory, and every entry that is a link, not root's, or writable by group
# or other removed. Contents anyone could have rewritten are not trusted after
# a chmod: a 0666 `pids` edited to name an unrelated root process would be
# killed by the next teardown. A 0.9.x window started under umask 000 loses its
# state with them, and children recorded only in its `pids` are no longer
# stopped for it; accepted, since that state is exactly what was untrusted.
# "Root" is whoever runs this: uid 0 in the field, the tester in the tests.
run_dir(){
  local bad
  # /run is root's, so a link in place of the directory is not ours to follow.
  if [[ -L $RUN ]]; then rm -f -- "$RUN" || return 1; fi
  mkdir -p -m 755 -- "$RUN" 2>/dev/null
  [[ -d $RUN && ! -L $RUN ]] || return 1
  bad=$(find "$RUN" -maxdepth 1 \( -type l -o ! -user "$EUID" -o -perm /022 \) -print -quit) \
    || return 1
  [[ -n $bad ]] || return 0
  [[ -O $RUN ]] || chown -h -- "$EUID" "$RUN" || return 1
  chmod go-w -- "$RUN" || return 1
  find "$RUN" -mindepth 1 -maxdepth 1 \( -type l -o ! -user "$EUID" -o -perm /022 \) \
    -exec rm -rf -- {} + || return 1
  log "repaired $RUN: took group and other write away, removed links and entries others owned or could write"
}
# need_run_dir: run_dir, or stop before root writes anything. Not die: its log
# would append to the very directory that could not be trusted.
need_run_dir(){
  run_dir && return 0
  printf '%s\n' "$(date -u +%H:%M:%SZ) $RUN is not a directory only root can write, and could not be made one" >&2
  exit 3
}

# Every value that reaches bash arithmetic -- $(( )), (( )), or a -lt/-gt
# comparison inside [[ ]] -- must pass this first. Arithmetic expands its
# operands as expressions, and an array subscript is expanded too, so a value
# such as a[$(cmd)] runs cmd. This script runs as root for any active user
# through polkit, with any argv they choose; the state files under $RUN are
# root-owned, but they are checked as well so that no arithmetic here depends
# on where its input came from.
#
# Canonical decimal only: bash reads a leading 0 as octal, so 010 is 8 and 08
# is an error, and a long enough digit string wraps negative.
#
# Two widths, so that a deadline computed from an accepted duration is always
# an accepted deadline. A duration has at most nine digits (about 31 years);
# now plus any duration stays within is_uint's twelve digits until the year
# 33,000, both where start computes it and where an extension asks date(1).
is_uint(){ [[ $1 =~ ^(0|[1-9][0-9]{0,11})$ ]]; }
is_duration(){ [[ $1 =~ ^(0|[1-9][0-9]{0,8})$ ]]; }

# --- firmware helpers -------------------------------------------------------
fwerr(){ dmesg | grep -o "firmware error -\?[0-9]*" | tail -1 | grep -o '\-\?[0-9]*$'; }
# On failure the firmware's reason is the whole diagnosis, and it used to go to
# /dev/null: 05:3xZ an awdl_payload SET was rejected on every window of the
# night and nothing said why. Keep stdout for the caller to log.
#
# `timeout --foreground` here, for awdl-up, and inside awdl-up: plain timeout
# moves itself and its child into a process group of their own, so the group
# SIGTERM/SIGKILL omdrop-core sends a cancelled or overdue hook missed them,
# and a SET or an interface create could land after `stop` had reported the
# radio off (security review, HOOK-TIMEOUT-PGRP-ESCAPE). --foreground only
# stops timing out the child's own children. brcm_iovar.py has none; awdl-up's
# running command outlives an expired awdl-up by at most its own 8 or 20 s.
iovar_set(){ local out rc; dmesg -C
             out=$(timeout --foreground 8 python3 "$LIB/brcm_iovar.py" -i "$IF" set "$1" "$2" 2>&1); rc=$?
             (( rc == 0 )) || log "iovar_set $1: rc=$rc ${out##*$'\n'}"
             return $rc; }
# reply_file <tag>: a new, empty file in $RUN for one GET's reply, under a name
# nobody can predict. The names used to be fixed (payload.bin,
# read.<iovar>.bin, diag.*.bin) and deleted before each GET, after which root
# had brcm_iovar.py open that name: in a run directory others could write, a
# link planted between the delete and the open made root overwrite any file
# with firmware bytes. mktemp creates the file itself, exclusively.
reply_file(){ mktemp "$RUN/$1.XXXXXX"; }
# iovar_get_len <iovar> <len> <file>: one GET into <file>, a fresh reply_file,
# printing the reply's size, 0 for none.
#
# brcm_iovar.py writes --raw only when the GET succeeds, so a leftover file
# from an earlier call reads as a live value: after a driver reload the
# template was gone, `awdl_payload` GET returned EROFS (AWDL disabled), and the
# stale 512-byte file from the previous window made the gate refuse the
# retoggle for a template that no longer existed. A fresh file is empty, so
# that cannot happen; one with no reply is removed, which leaves the caller
# exactly what the old delete-then-GET did.
iovar_get_len(){ local n
                 timeout --foreground 8 python3 "$LIB/brcm_iovar.py" -i "$IF" get "$1" -l "$2" --raw "$3" >/dev/null 2>&1
                 n=$(stat -c %s "$3" 2>/dev/null) || n=0
                 [[ $n == 0 ]] && rm -f "$3"
                 echo "$n"; }
# iovar_len <iovar> <len>: the size of one GET's reply, 0 for none; the reply
# itself is not kept. 0 too when no reply file can be made, as when the old
# fixed name could not be written.
iovar_len(){ local f n
             f=$(reply_file "$1") || { echo 0; return 0; }
             n=$(iovar_get_len "$1" "$2" "$f"); rm -f "$f"; echo "$n"; }
le32(){ python3 -c "import struct,sys;print(struct.pack('<I',int(sys.argv[1])).hex())" "$1"; }

awdl0_ready(){
  ip link show "$IF" >/dev/null 2>&1 || return 1
  ip link show "$IF" | grep -q 'state UP\|UP,' || ip link set "$IF" up 2>/dev/null
  ip link show "$IF" >/dev/null 2>&1
}

# "Is brcmfmac there at all", which is a different question from "is awdl0
# there". A Wi-Fi off/on removes the vif and leaves the module loaded, and the
# two used to be conflated into one wrong message.
driver_loaded(){ [[ -d /sys/module/brcmfmac ]]; }

# The probe hook's sentence for a missing radio, or nothing when it reports
# none or cannot be run. It tells a driver that is not loaded from one that is
# not built for this kernel, or is the kernel's own and has no AWDL (issue 51);
# asking it keeps doctor and start giving the same answer.
driver_verdict(){
  local out
  out=$(timeout --foreground 10 "$LIB/radio/probe" --json 2>/dev/null) || return 0
  python3 -c 'import json, sys
for m in json.loads(sys.argv[1]).get("missing", []):
    if m.get("id") == "radio":
        print(m["say"])
        break' "$out" 2>/dev/null
  return 0
}

# NetworkManager's view of awdl0, e.g. "10 (unmanaged)"; empty when there is
# no NetworkManager or it does not know the device.
nm_state(){
  command -v nmcli >/dev/null 2>&1 || return 0
  systemctl is-active --quiet NetworkManager 2>/dev/null || return 0
  nmcli -g GENERAL.STATE device show "$IF" 2>/dev/null
}

# The same check awdl-up makes: a managed awdl0 gets no link-local, and the
# package's unmanaged setting is only in force once NetworkManager has reread
# its configuration (issue 15). `reload conf` touches no connection.
nm_release(){
  local state; state=$(nm_state)
  [[ -z $state || $state == *'(unmanaged)'* ]] && return 0
  if nmcli general reload conf >/dev/null 2>&1; then
    log "NetworkManager was managing $IF ($state); reloaded its configuration: $(nm_state)"
  else
    log "NetworkManager is managing $IF ($state) and would not reload its configuration"
  fi
}

# The only honest test of the data path: submit a burst and count the per-frame
# completions the firmware logs. A parked firmware parses PSFs, fills its peer
# table and elects normally while transmitting nothing, so peer state, opmode
# and outstanding_tx all look healthy in both cases. >=15 of 20 = live.
#
# Those completions are exactly what `awdl_trace` gates, and it is off by
# default -- so the probe turns it on for its own three seconds and puts it
# back. Without this the gate reads 0 completions on a perfectly healthy radio
# and every window fails as parked (2026-09-18: that is what happened for
# twenty minutes after the parameter shipped).
#
# The probe frames are AirDrop announcements, unless PROBE_NEUTRAL is set: the
# boot warm-up runs the gate with no window open, and sends the same frames
# with no records, so it announces no service and no host name.
TRACE=/sys/module/brcmfmac/parameters/awdl_trace
probe_completions(){
  local was= n
  if [[ -w $TRACE ]]; then was=$(cat "$TRACE" 2>/dev/null); printf 1 > "$TRACE"; fi
  dmesg -C
  if ! python3 "$LIB/awdl-airdrop-adv.py" --iface "$IF" --count 20 --interval 0.02 --seq 9000 \
       ${PROBE_NEUTRAL:+--neutral} >/dev/null 2>&1; then
    [[ -n $was ]] && printf '%s' "$was" > "$TRACE"
    return 1
  fi
  sleep 3
  n=$(dmesg | awk '/awdl txstatus/' | grep -c 'tx_status=0x' || true)
  [[ -n $was ]] && printf '%s' "$was" > "$TRACE"
  printf '%s' "$n"
}

# Whether the loaded brcmfmac can log completions: prints yes, no or unknown.
#
# yes: the module has the `awdl_trace` parameter, or it is an older build that
#   printed every completion unconditionally. Both print the line through
#   pr_info() with a "brcmfmac: " prefix in the format string itself.
# no: aurora-silicon/linux at bc1823a897a7 moved the line to brcmf_dbg(), with
#   no prefix and no parameter. That is no_printk() in a release build, and in
#   a DEBUG build it prints only with the MSGBUF bit in the `debug` mask, which
#   nothing here sets. Either way the probe reads 0 on a healthy radio, and the
#   gate would fail every window as parked.
# unknown: the module file can't be read, or it is not the module that is
#   loaded (a package install replaces the file; the old module runs until a
#   reload). The caller keeps the probe, as before this check existed.
#
# Loaded and on-disk modules are matched by build ID: the kernel exposes the
# loaded module's .note.gnu.build-id verbatim in sysfs, and the same bytes sit
# in the file. brcmfmac has no MODULE_VERSION, and without
# CONFIG_MODULE_SRCVERSION_ALL the builds tested here expose no srcversion to
# compare. Python reads the whole stream, so no reader exits early and kills
# the decompressor with SIGPIPE under pipefail; every stage's status is checked.
NOTE=/sys/module/brcmfmac/notes/.note.gnu.build-id
trace_support(){
  [[ -e $TRACE ]] && { echo yes; return; }
  local ko out ans st
  local py='import sys
note = open(sys.argv[1], "rb").read()
data = sys.stdin.buffer.read()
if len(note) < 16 or note not in data:
    print("unknown")
else:
    print("yes" if b"brcmfmac: awdl txstatus" in data else "no")'
  ko=$(modinfo -n brcmfmac 2>/dev/null) && [[ -r $ko && -r $NOTE ]] || { echo unknown; return; }
  # Each branch prints the answer, then every stage's exit status on a second
  # line: PIPESTATUS doesn't survive a command substitution.
  case $ko in
    *.zst) out=$(zstd -dcq -- "$ko" 2>/dev/null | python3 -c "$py" "$NOTE"; echo "${PIPESTATUS[*]}") ;;
    *.xz)  out=$(xz -dc -- "$ko" 2>/dev/null | python3 -c "$py" "$NOTE"; echo "${PIPESTATUS[*]}") ;;
    *.gz)  out=$(gzip -dc -- "$ko" 2>/dev/null | python3 -c "$py" "$NOTE"; echo "${PIPESTATUS[*]}") ;;
    *)     out=$(python3 -c "$py" "$NOTE" < "$ko" 2>/dev/null; echo "0 $?") ;;
  esac
  ans=${out%%$'\n'*} st=${out##*$'\n'}
  read -r -a st <<<"$st"
  (( ${#st[@]} == 2 && st[0] == 0 && st[1] == 0 )) && [[ $ans == @(yes|no|unknown) ]] \
    || { echo unknown; return; }
  echo "$ans"
}

# Every exit that opens a window starts the receive sample from here.
open_on_transmit(){
  rx_baseline
  log "gate: $1 transmits; opening with receive unproven until data arrives"
}

# Returns 0 when the path TRANSMITS. That is the whole question the gate can
# answer in an empty room, and it takes about 3 s: a parked firmware parses
# PSFs, fills its peer table and elects normally while transmitting nothing, so
# nothing else in the radio's own state distinguishes the two.
#
# The receive half parks independently and is NOT waited for here (01:52Z: a
# quiet room is not a parked radio, and the wait burned the gate's whole budget
# on a radio transmitting 20/20). It is sampled by the supervisor and reported
# through rx_proven, so a panel can tell "advertising" from "answered".
#
# Retoggles awdl=0/1 only while NO template is loaded: awdl=0 with awdl_payload
# set hung the firmware (023513Z), and that dead end needs a module reload.
gate(){
  local n try ts
  rx_reset
  ts=$(trace_support)
  if [[ $ts == no ]]; then
    log "gate: this brcmfmac cannot log AWDL tx completions; opening with transmit and receive unproven"
    rx_baseline
    return 0
  fi
  [[ $ts == unknown ]] && log "gate: could not tell whether this brcmfmac logs tx completions; probing anyway"
  n=$(probe_completions) || return 2
  log "gate: probe 20 frames -> $n completions"
  if [[ ${n:-0} -ge 15 ]]; then open_on_transmit probe; return 0; fi
  local plen; plen=$(iovar_len awdl_payload 512)
  if [[ ${plen:-0} -gt 0 ]]; then
    log "gate: template loaded (awdl_payload len $plen); no awdl=0 retoggle (023513Z)"
    return 1
  fi
  for try in $(seq 1 "$ENABLES"); do
    iovar_set awdl "$(le32 0)"; sleep 2
    iovar_set awdl "$(le32 1)"; sleep 3
    ip link set "$IF" promisc on 2>/dev/null; ip link set "$IF" allmulticast on 2>/dev/null
    for wait in 7 30; do
      sleep "$wait"
      n=$(probe_completions) || return 2
      log "gate: enable $try, probe 20 frames -> $n completions"
      if [[ ${n:-0} -ge 15 ]]; then open_on_transmit "enable $try"; return 0; fi
    done
  done
  return 1
}

# self_metric: the election metric this machine runs, and therefore the one our
# PSFs must advertise. $CONF/election-metric overrides the default of 100.
self_metric(){ local em; em=$(head -1 "$CONF/election-metric" 2>/dev/null | tr -dc '0-9'); printf '%s' "${em:-100}"; }

# election_state: the firmware's own view, as "<master> <sync> <depth> <master
# metric> <self metric>". mkpsf takes these on its command line.
#
# The self metric was hardcoded to 100 here, and that is a bug with teeth: with
# awdl_election_metric=600 in the firmware we refuse to adopt anyone, while our
# template still told the room we were worth 100 -- so nobody adopted us
# either. Measured exactly that dead end: a peer advertising a top master at
# distance 0 and metric 510, us root of our own tree, two disjoint trees,
# action frames crossing and no data. Advertise what we actually run.
election_state(){
  local etree opmode rc
  etree=$(reply_file etree) || return 1
  opmode=$(reply_file opmode) || { rm -f "$etree"; return 1; }
  iovar_get_len awdl_election_tree 42 "$etree" >/dev/null
  iovar_get_len awdl_opmode 12 "$opmode" >/dev/null
  python3 - "$etree" "$opmode" "$(cat /sys/class/net/$IF/address)" "$(self_metric)" "$LIB" <<'PYX'
import os, re, subprocess, sys
etree, opmode, us, mine, lib = sys.argv[1:6]
txt = subprocess.run([sys.executable, os.path.join(lib, 'awdl-election.py'), etree],
                     capture_output=True, text=True).stdout
top = re.search(r'top_master\s*=\s*([0-9a-f:]{17})\s+metric\s+(\d+)', txt)
dep = re.search(r'tree_depth\s*=\s*(\d+)', txt)
try:
    b = open(opmode, 'rb').read()
    sync = ':'.join(f'{x:02x}' for x in b[4:10]) if len(b) >= 10 else us
except OSError:
    sync = us
master, mm = (top.group(1), top.group(2)) if top else (us, mine)
# Rooted on ourselves: the master metric IS our metric, not whatever the tree
# readback happened to hold.
if master == us:
    mm = mine
depth = dep.group(1) if dep else '0'
print(master, sync, depth, mm, mine)
PYX
  rc=$?
  rm -f "$etree" "$opmode"
  return "$rc"
}

# write_template <state...>: the PSF template for one election state.
#
# $CONF/no-template suppresses the write. It exists because two runs
# measured reception dying at the exact second this runs: rx_packets climbed at
# 2-4/s through the whole gate phase and froze permanently within four seconds
# of the template SET, twice, with the RSSI-threshold and metric writes ruled
# out by the same measurement. A window with the template suppressed is not
# discoverable -- our PSFs carry no election state -- so this is a diagnostic
# switch, not an operating mode.
write_template(){
  local body header
  [[ -e $CONF/no-template ]] && {
    log "template write suppressed ($CONF/no-template)"; return 0; }
  body=$(python3 "$LIB/mkpsf.py" --body --template --variant E \
           --master "$1" --sync "$2" --distance "$3" \
           --master-metric "$4" --self-metric "$5")
  # The iovar takes a u16 LE length header followed by the PSF body. The
  # working harness has always sent `le16(len) + body`; this function shipped
  # without the header once and every SET since was rejected EFAULT -- the
  # firmware reads a length that is not there. Found in our own published
  # source, after a night of negatives.
  #
  # A write nobody checks is a write that can fail forever in silence:
  # `awdl_payload` GET returned BCME_NOTFOUND (-30) inside live windows whose
  # log said "template rewritten" every interval. A radio emitting bare default
  # PSFs advertises no HT IE, so no Mac builds a peer-cache entry for us, so
  # every Mac discards its own unicast to us before the air.
  header=$(python3 -c "import struct;print(struct.pack('<H', $(( ${#body} / 2 ))).hex())")
  if ! iovar_set awdl_payload "${header}${body}"; then
    log "TEMPLATE WRITE FAILED: awdl_payload SET rejected by the firmware"
    return 1
  fi
  return 0
}

# track_election: rewrite the template whenever the firmware's election moves.
# The root DOES move mid-window -- observed changing from one peer to another while a
# window was live -- and a template naming a root we no longer sync to
# advertises a schedule we do not keep. Apple's driver rewrites from its own
# election path; the research harness does it every interval; doing it once at
# window start was the difference. The supervisor calls this each interval.
# Hysteresis: the root FLAPS. Observed going self -> peer A -> peer B
# in seven seconds, and rewriting on every observation turns each flap into an
# awdl_payload SET -- churn on the one firmware path with a recorded hang
# (023513Z), for a root that is about to change again. A state has to be
# observed twice in a row before it is worth advertising. The first write after
# a reset (election.state absent) is immediate: the window needs a template.
track_election(){
  local state previous pending
  state=$(election_state) || return 0
  previous=$(cat "$RUN/election.state" 2>/dev/null || echo "")
  [[ $state == "$previous" ]] && { rm -f "$RUN/election.pending"; return 0; }
  if [[ -n $previous ]]; then
    pending=$(cat "$RUN/election.pending" 2>/dev/null || echo "")
    if [[ $state != "$pending" ]]; then
      printf '%s\n' "$state" > "$RUN/election.pending"
      return 0                       # first sighting: wait for confirmation
    fi
  fi
  rm -f "$RUN/election.pending"
  # shellcheck disable=SC2086
  # Only record and announce a rewrite that the firmware accepted. Recording it
  # on a failed write is worse than useless: election.state then says we are
  # advertising a state we are not, and the next interval sees "no change" and
  # never retries.
  if ! write_template $state; then
    set -- $state
    log "template NOT rewritten (write failed): master=$1 sync=$2 depth=$3"
    return 1
  fi
  printf '%s\n' "$state" > "$RUN/election.state"
  set -- $state
  log "template rewritten: master=$1 sync=$2 depth=$3 (was: ${previous:-none})"
}

# write_rssi_thresholds <dBm>: GET the 42-byte election_tree, patch bytes 7, 9,
# 10 and 11 (close_sync, edge_sync, close_range, mid_range -- byte 8,
# master_rssi_boost, is deliberately untouched) and SET it back. Offsets from
# setAWDL_ELECTION_RSSI_THRESHOLDS in the kernelcache; the harness has written
# them this way since 09-09.
#
# Why the button needs them: at the firmware default (-80) we consider distant
# strangers as adoptable roots, which is how a window ends up rooted on a peer
# at -75 dBm while the Mac in the same room sits at -50 in its own tree. -60 keeps the
# election to peers that are actually in the room.
write_rssi_thresholds(){
  local t=$1 before payload rc
  [[ ${#t} -gt 0 ]] || return 0
  before=$(reply_file etree.rssi) || return 0
  [[ $(iovar_get_len awdl_election_tree 42 "$before") -eq 42 ]] || {
    rm -f "$before"; log "rssi thresholds: readback was not 42 bytes; not writing"; return 0; }
  payload=$(python3 - "$before" "$t" <<'PYX'
import sys
b = bytearray(open(sys.argv[1], 'rb').read())
t = int(sys.argv[2]) & 0xff
for off in (7, 9, 10, 11):
    b[off] = t
print(bytes(b).hex())
PYX
)
  rc=$?
  rm -f "$before"
  [[ $rc == 0 ]] || return 0
  iovar_set awdl_election_tree "$payload" && log "identity: rssi thresholds ${t} dBm"
}

# Write the service TLVs (bare hex) to awdl_afs_pload, trying both formats
# measured in the field; returns 0 on the first the firmware accepts. The
# header form is le16(4) + le16(total) + TLVs with total = 4 + body length:
# the firmware copies total-4 bytes. Measured on BCM4388 Apple firmware
# 23.20.95 (Jun 2023): le16(4)+le16(body) was accepted but stored a block
# truncated by 4 bytes -- the TXT record lost its tail (RAM-dump verified:
# "flags=136" stored as "flags"+4 garbage); le16(4)+le16(4+body) stores the
# full block byte-identical.
afs_pload_write(){
  local hdr
  if iovar_set awdl_afs_pload "$1"; then
    log "identity: afs_pload $(( ${#1} / 2 )) B written (bare TLVs)"
    return 0
  fi
  hdr=$(python3 -c "import struct,sys;print((struct.pack('<HH',4,4+len(bytes.fromhex(sys.argv[1])))+bytes.fromhex(sys.argv[1])).hex())" "$1")
  if iovar_set awdl_afs_pload "$hdr"; then
    log "identity: afs_pload $(( ${#1} / 2 )) B written (le16(4)+le16(4+len) header)"
    return 0
  fi
  return 1
}
# Our AWDL identity on air: the PSF template (so our sync frames are well
# formed) and the Arpa + _airdrop._tcp service TLVs Apple reads out of MIFs.
write_identity(){
  # Election configuration FIRST, template second: the template advertises the
  # election state, so writing it before the metric and the RSSI thresholds
  # publishes a state the firmware is about to leave.
  #
  # Three failed windows are the argument for both settings. At metric 600 we
  # adopt nobody and nobody adopted us -- the peer Mac stayed Master at 510 and
  # never saw us. At metric 100 with default thresholds we adopted whatever we
  # could hear, including a peer at -75 dBm, and flapped between roots every few
  # seconds. Every window that actually carried a transfer had metric 100 AND had
  # adopted a peer that was in the room.
  local em; em=$(self_metric)
  iovar_set awdl_election_metric "$(le32 "$em")" && log "identity: election_metric=$em"
  write_rssi_thresholds "$(head -1 "$CONF/rssi-sync-threshold" 2>/dev/null | tr -dc '0-9-')"
  sleep 3                          # let the election settle before we publish it
  rm -f "$RUN/election.state"      # force the first template write
  track_election
  # $CONF/no-service and $CONF/no-aftxmode: the last two writes of the startup
  # sequence, separated because reception dies inside the same second they run
  # (03:16:15Z, with the template already suppressed and the metric and RSSI
  # writes ruled out). Diagnostic switches: without the service TLV nothing can
  # resolve us, so a window with either set is a measurement, not a usable one.
  # NOTE THE ASYMMETRY, measured: awdl_payload takes `le16(len) + body` and
  # rejects a bare body with BCME_BUFTOOSHORT (-14, printed EFAULT).
  # awdl_afs_pload is firmware-variant: the firmware this was first measured
  # on takes the TLVs BARE and rejects a length-prefixed buffer with
  # BCME_BUFTOOLONG (-15, printed ENOTBLK), while the BCM4388 Apple firmware
  # 23.20.95.0.40.50.92 (Jun 2023, MBP 16-inch 2023) rejects the bare TLVs
  # with BCME_BADLEN (-24, printed EMFILE) for any buffer >= 8 B (smaller
  # gives BCME_BUFTOOSHORT) and wants `le16(4) + le16(total) + TLVs` with
  # total = 4 + body length: it copies total-4 bytes, so le16(body) silently
  # stores the block truncated by 4 bytes (RAM-dump verified: the TXT record
  # lost its "=136" tail) while le16(4 + body) stores it byte-identical. The
  # first u16 is not content-checked (0, 4 and 8 all pass). So try bare
  # first (the measured-good form for the original firmware) and fall back
  # to the header form for the firmware that wants it.
  #
  # These TLVs are where a Mac reads our _airdrop._tcp advert: with them a peer
  # Mac resolves us and tiles in 3.0 s.
  local svc
  svc=$(python3 "$LIB/mkpsf.py" --service-only)
  if [[ -e $CONF/no-service ]]; then
    log "identity: afs_pload suppressed ($CONF/no-service)"
  elif afs_pload_write "$svc"; then
    :
  else
    log "SERVICE WRITE FAILED: awdl_afs_pload SET rejected by the firmware"
  fi
  if [[ -e $CONF/no-aftxmode ]]; then
    log "identity: aftxmode suppressed ($CONF/no-aftxmode)"
  else
    iovar_set awdl_aftxmode "$(le32 1)" && log "identity: aftxmode=1"
  fi
}

# nudge_peers: send one unicast packet to every pinned AWDL neighbour, every
# interval.
#
# Why this is not optional. Solicited-node multicast does not cross AWDL
# (measured: 513 NS from us, 0 NA; the peer's capture received frames to
# 33:33:00:00:00:fb and none to 33:33:ff:xx:xx:xx). So an Apple device can
# resolve our NAME over mDNS, get our AAAA, and then be unable to resolve the
# ADDRESS to a MAC -- its neighbour discovery goes out to a group we never
# receive. A peer Mac showed exactly that: `ndp -an` held every other device on
# the link and no entry at all for our AWDL address, and a scoped ping6 returned
# "No route to host". We pin THEIR addresses (awdl-peer-watch); nothing was
# making them learn OURS.
#
# One unicast frame from us does it: the peer learns the mapping from the
# frame's source. The research harness has pinged its peer every interval and
# its windows were the ones Apple devices could see; the product path never did,
# and its windows were not. Cheap, and it needs no reply to work.
nudge_peers(){
  local ll
  for ll in $(ip -6 neigh show dev "$IF" 2>/dev/null | awk '/PERMANENT/{print $1}'); do
    ping -6 -c 1 -W 1 -I "$IF" "$ll" >/dev/null 2>&1 &
  done
}

# radio_up: everything `start` does to the radio before it claims a window --
# rebuild a missing awdl0, release it from NetworkManager, run the gate, and
# wait for the link-local. Moved unchanged out of cmd_start, except that the
# window's reason goes through set_reason; exits through die with 4 (no awdl0)
# or 3 (parked).
radio_up(){
  # awdl0 goes away for two very different reasons, and they used to print the
  # same sentence: "the AWDL driver is not loaded".
  #
  # The common one is not a driver problem at all. Toggling Wi-Fi off and on
  # destroys the AWDL vif while brcmfmac stays loaded, so the machine is one
  # `awdl-if create` away from working -- about two seconds, no reload, no cost
  # to the Wi-Fi link. Telling the user their driver is missing, and refusing,
  # was wrong on the diagnosis and wrong on the remedy. Rebuild the interface
  # and carry on.
  #
  # A rebuild needs a driver with AWDL loaded, though. When the probe names
  # what is missing instead -- no driver, or one without AWDL -- retrying the
  # create cannot help, so say that.
  if ! awdl0_ready; then
    local verdict; verdict=$(driver_verdict)
    if [[ -n $verdict ]]; then
      set_reason no_awdl0
      die 4 "$verdict"
    fi
  fi
  if ! awdl0_ready && driver_loaded; then
    log "$IF is missing but the driver is loaded (a Wi-Fi off/on removes it); rebuilding it"
    timeout --foreground 60 "$LIB/awdl-up" >/dev/null 2>&1 || true
    awdl0_ready || sleep 2
  fi

  if ! awdl0_ready; then
    set_reason no_awdl0
    # No paths, no iovar names: this is read by someone holding a laptop.
    if driver_loaded; then
      die 4 "AirDrop's radio interface could not be started. Turning Wi-Fi off and back on usually clears this; a restart always does."
    fi
    die 4 "AirDrop's Wi-Fi driver is not loaded, so this machine cannot be discovered. Restart to load it."
  fi
  # An awdl0 that already exists skips awdl-up, so check NetworkManager here
  # too: installed on a running system, the package's unmanaged setting is not
  # in force until NetworkManager rereads its configuration (issue 15). Before
  # the gate, whose awdl toggle recreates the netdev, so the new one is left
  # alone and gets its link-local.
  nm_release
  ip link set "$IF" promisc on 2>/dev/null; ip link set "$IF" allmulticast on 2>/dev/null

  gate; GATE_RC=$?
  case "$GATE_RC" in
    0) : ;;
    2) set_reason parked
       log "gate: data-path probe inconclusive; firmware state unclassified"
       die 3 "AirDrop's radio did not answer a test. Try again in a moment." ;;
    *) set_reason parked
       # Two different dead ends, and telling the user to "just retry" in the
       # second one is a lie: a retry five times over twelve minutes got a
       # byte-identical failure each time. When a template is loaded the gate
       # refuses the awdl=0 retoggle (023513Z), which is the ONLY thing that
       # clears a parked path -- so it can never recover itself. The template
       # cannot be cleared either: a zero-length awdl_payload SET is rejected
       # BCME_BADARG, and any replacement write still reads back 512 bytes, so
       # the guard still sees "loaded". A driver reload is the only exit.
       # The panel shows the last line, so the evidence goes above it and the
       # sentence a person reads goes last. Naming a reload in a panel is
       # useless -- there is nothing to press -- and a restart clears the same
       # state without a terminal.
       if [[ $(iovar_len awdl_payload 512) -gt 0 ]]; then
         log "parked with a template loaded (023513Z): the gate cannot retoggle awdl, retrying is byte-identical. Reload to clear: pkexec $LIB/awdl-up --reload (~15 s of Wi-Fi)."
         die 3 "AirDrop's radio is stuck and cannot restart itself. Restarting this machine clears it."
       fi
       log "parked after $ENABLES enables; no template loaded, so a retoggle can still clear it"
       die 3 "AirDrop's radio would not start transmitting. Try again -- a second attempt usually works." ;;
  esac

  # The gate toggles awdl=0/1, and every disable drops the netdev: the link
  # awdl0_ready brought up above is DOWN again by the time the gate returns,
  # and nothing else re-ups it. That shipped a window advertising on a dead
  # interface -- no IFF_UP, no link-local, the receiver unable to bind, and
  # `reason` still ok (observed 07:57:30Z and 08:15:56Z, on both bands, after
  # the firmware itself was provably fine: afrx/aftx kept climbing throughout).
  # Re-assert the link and require the address before claiming the window, so
  # a dead netdev reports no_awdl0 instead of lying with ok.
  ip link set "$IF" up 2>/dev/null
  local w
  for w in $(seq 1 30); do
    ip -6 addr show dev "$IF" scope link 2>/dev/null | grep -q inet6 && break
    sleep 0.5
  done
  if ! ip -6 addr show dev "$IF" scope link 2>/dev/null | grep -q inet6; then
    set_reason no_awdl0
    if [[ $(nm_state) != '' && $(nm_state) != *'(unmanaged)'* ]]; then
      die 4 "awdl0 has no link-local because NetworkManager is managing it ($(nm_state)). Run: sudo nmcli general reload conf, then try again"
    fi
    die 4 "awdl0 has no link-local 15 s after the gate -- the data path is up but nothing can bind to it"
  fi
}

# radio_tick: the radio's share of every supervision interval. Moved unchanged
# out of the supervisor loop.
radio_tick(){
  # Anything that resets the radio -- an infra re-association, an rfkill
  # toggle, a firmware reset -- drops awdl0 while the window is still open,
  # and the frames below then leave on an interface nothing can bind to
  # (08:26:12Z: status ok, receiver activating, awdl0 DOWN). Re-assert it
  # every interval: `ip link set up` on an already-up link is a no-op, so
  # this costs nothing when the link is healthy and restores it when it
  # is not.
  if ! ip -6 addr show dev "$IF" scope link 2>/dev/null | grep -q inet6; then
    log "awdl0 lost its link-local mid-window -- re-asserting the link"
    ip link set "$IF" up 2>/dev/null
    ip link set "$IF" promisc on 2>/dev/null
    ip link set "$IF" allmulticast on 2>/dev/null
  fi
  track_election
}

# peers: the firmware's own view of who is on air -- address, RSSI, self
# metric, and crucially the root EACH PEER ADVERTISES (top_master/dist). Two
# devices can only exchange data inside a tree they share, so "which root is
# that Mac on" is the question that separates "we are invisible" from "we are in a
# different tree". Read-only: one GET, no SET, no toggles.
#
# Each call gets its own reply file. The panel reads the table every couple of
# seconds while a name lookup and a send read it too, and with one shared file
# a call's delete-before-read (iovar_get_len used to delete first) wiped
# another's reply mid-read: 5 of 12 concurrent calls failed "no reply" on
# 2026-09-24.
cmd_peers(){
  need_root peers
  need_run_dir
  local raw len rc
  raw=$(reply_file advertisers) || { echo "awdl_advertisers: cannot create a reply file in $RUN" >&2; exit 4; }
  len=$(iovar_get_len awdl_advertisers 1024 "$raw")
  if [[ ${len:-0} -gt 0 ]]; then
    python3 "$LIB/awdl-peers.py" "$raw"; rc=$?
  else
    echo "awdl_advertisers: no reply (is AWDL enabled?)" >&2; rc=4
  fi
  rm -f "$raw"
  exit "$rc"
}

# diag: why is the receive side dead? Read-only -- GETs, no SET, no toggle, so
# it is safe to run inside a live window and it cannot perturb what it
# measures. Two awdl_stats snapshots <seconds> apart, plus the schedule the
# firmware is actually keeping and the master it is actually synced to.
#
# The question it answers: 20/20 completions with zero inbound data frames has
# two very different causes, and they need different fixes.
#
#   afrx climbing, datarx flat, misalign/lostmaster/noawchansw climbing
#       -> we are not on the root's schedule. The frames are not arriving
#          because the radio is elsewhere when they are sent.
#   afrx and rx80211 climbing, datarx flat, rxdrop/awdrop/afrxdrop climbing
#       -> the frames arrive and the firmware discards them.
#   nothing climbing at all
#       -> the receive path is parked, not merely misaligned.
#
# `noawchansw` is the one to watch on this machine: the master AW is channel 6
# while the infra link sits on 44, so every master AW costs a band switch.
cmd_diag(){
  need_root diag
  need_run_dir
  local secs=${1:-15} rx0 rx1 opm= chs= syn= st0= st1=
  is_uint "$secs" && (( secs <= 600 )) || die 2 "usage: $0 diag [seconds, at most 600]"
  opm=$(reply_file diag.opmode) && chs=$(reply_file diag.chanseq) && syn=$(reply_file diag.sync) \
    && st0=$(reply_file diag.stats.0) && st1=$(reply_file diag.stats.1) || {
    rm -f -- $opm $chs $syn $st0 $st1
    echo "diag: cannot create reply files in $RUN" >&2; exit 4; }
  iovar_get_len awdl_opmode 12 "$opm" >/dev/null
  iovar_get_len awdl_chan_seq 64 "$chs" >/dev/null
  iovar_get_len awdl_sync_params 64 "$syn" >/dev/null
  python3 - "$opm" "$chs" "$syn" <<'PYX'
import struct, sys
opmode, chanseq, sync = sys.argv[1:4]
def load(path):
    try:
        return open(path, 'rb').read()
    except OSError:
        return b''
b = load(opmode)
if len(b) >= 10:
    mode, role, bcast = struct.unpack_from('<BBH', b, 0)
    master = ':'.join(f'{x:02x}' for x in b[4:10])
    print(f'opmode      mode={mode} role={role} bcast={bcast}  sync_master={master}')
else:
    print(f'opmode      unreadable ({len(b)} B)')
b = load(sync)
if len(b) >= 36:
    f = struct.unpack_from('<BHBHBBHHHHHH', b, 0)
    print(f'sync_params master_chan={f[4]} aw_period={f[6]} af_period={f[7]} '
          f'flags=0x{f[8]:04x} ext={f[9]} common={f[10]} presence={b[30]}')
else:
    print(f'sync_params unreadable ({len(b)} B)')
b = load(chanseq)
if len(b) >= 38:
    slots = [b[6 + 2 * i] for i in range(16)]
    print('chan_seq    ' + ' '.join(f'{c:>3}' for c in slots))
else:
    print(f'chan_seq    unreadable ({len(b)} B)')
PYX
  iovar_get_len awdl_stats 324 "$st0" >/dev/null
  rx0=$(cat "/sys/class/net/$IF/statistics/rx_packets" 2>/dev/null || echo 0)
  sleep "$secs"
  iovar_get_len awdl_stats 324 "$st1" >/dev/null
  rx1=$(cat "/sys/class/net/$IF/statistics/rx_packets" 2>/dev/null || echo 0)
  # Both directions. --rx hid aftx, and 04:5xZ that was the field in question:
  # a Mac only schedules unicast to a peer in its table, and its table is built
  # from our ACTION frames -- so "are we emitting any" is as important as what
  # we receive.
  python3 "$LIB/awdl-stats.py" "$st0" "$st1"
  printf 'netdev rx_packets %s -> %s over %ss (%+d)\n' "$rx0" "$rx1" "$secs" "$(( rx1 - rx0 ))"
  rm -f "$opm" "$chs" "$syn" "$st0" "$st1"
  exit 0
}

# read <iovar> [len]: one GET of an AWDL iovar, hexdumped. GET-only by
# construction -- there is no code path here that issues a SET, and the verb
# name carries no value argument to set with.
#
# Why it exists: the investigation reached "what did the firmware actually
# store when we wrote awdl_peer_op ADD for that peer?", and nothing could
# answer it. brcm_iovar.py needs root, root here means pkexec, and only this
# script is allowlisted -- so every readback of a new iovar cost a password
# prompt. GETs are the whole diagnostic vocabulary of this project; they should
# not need a human.
#
# Only AWDL state and the firmware's version and capability strings. Any active
# user reaches this as root without a prompt, and other iovars carry the Wi-Fi
# network's credentials (wsec_key, pmk, sae_password); nothing here shows the
# firmware refuses to read those back.
cmd_read(){
  local name=${1:-} len=${2:-512}
  [[ -n $name ]] || die 2 "usage: $0 read <iovar> [len]"
  [[ $name =~ ^awdl_[a-z0-9_]+$ || $name == ver || $name == cap ]] \
    || die 2 "read is limited to awdl_* iovars, ver and cap"
  is_uint "$len" && (( len <= 8192 )) || die 2 "len must be a number up to 8192"
  need_root read "$name"
  need_run_dir
  local raw got
  raw=$(reply_file "read.$name") || { echo "$name: cannot create a reply file in $RUN" >&2; exit 4; }
  got=$(iovar_get_len "$name" "$len" "$raw")
  [[ ${got:-0} -gt 0 ]] || { rm -f "$raw"; echo "$name: no reply (unsupported, or AWDL disabled)" >&2; exit 4; }
  echo "$name: $got bytes"
  od -An -tx1 -v "$raw" | sed 's/^ /  /' | head -64
  rm -f "$raw"
  exit 0
}

# The per-frame AWDL log lines, on demand. They are the only oracle for what
# the firmware does with a submitted frame -- a parked radio completes nothing
# -- and they are off by default because a discoverable window submits 40
# frames per interval. The probe borrows them for three seconds; this is for a
# human diagnosing a send, where they need to stay on for the attempt.
cmd_trace(){
  local want=${1:-}
  need_root trace "$want"
  [[ -e $TRACE ]] || die 4 "this module has no awdl_trace parameter; it predates it"
  case "$want" in
    on)  printf 1 > "$TRACE" ;;
    off) printf 0 > "$TRACE" ;;
    ''|status) : ;;
    *)   die 2 "usage: $0 trace [on|off]" ;;
  esac
  echo "awdl_trace=$(cat "$TRACE")"
  exit 0
}

# --- the radio backend hooks ------------------------------------------------
# omdrop-discoverable owns the window's state files, and these are where the
# moved code touches them: the reason a start failed, and the receive proof's
# reset and baseline. It defines all three. Under omdrop-core the hooks run
# with core owning those files (docs/backend-contract.md, "Run directory"), so
# hook_mode makes them no-ops.
#
# hook_mode: every hook calls it right after sourcing this file. Core copies a
# hook's stderr into $RUN/log with the timestamp kept, so a hook logs to stderr
# only; appending to the file as well would log every line twice.
# Whether the boot-time warm-up (awdl-warmup) holds the start lock: it records
# its pid in $RUN/warmup only while it holds it. A warm-up still queued for the
# lock does not name itself, so a start that finds an ordinary start holding
# the lock never waits for that one.
warmup_running(){
  local pid
  pid=$(cat "$RUN/warmup" 2>/dev/null) || return 1
  [[ $pid =~ ^[0-9]+$ ]] || return 1
  # comm is the interpreter's name for a `#!/usr/bin/env bash` script, so the
  # command line is what says it is awdl-warmup. A reused pid fails this.
  local arg
  while IFS= read -r -d '' arg; do
    [[ ${arg##*/} == awdl-warmup ]] && return 0
  done 2>/dev/null < "/proc/$pid/cmdline"
  return 1
}

hook_mode(){
  log(){ printf '%s\n' "$(date -u +%H:%M:%SZ) $*" >&2; }
  set_reason(){ :; }
  rx_reset(){ :; }
  rx_baseline(){ :; }
}
