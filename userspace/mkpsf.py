#!/usr/bin/env python3
"""Build an AWDL Periodic Synchronization Frame body, field by field.

Validated against Wireshark's AWDL dissector (packet-awdl.c, contributed by
SEEMOO's Milan Stute): all seven TLVs parse with zero malformed, and every field
decodes with correct semantics -- Master Channel 6, AW Period 16 TU, Presence
Mode 4, Step Count 3, Self Metric 60, Country Code X0, Social Channel Map =
channel 6.

    mkpsf.py --body   # hex of the AWDL body
    mkpsf.py          # text2pcap-able full 802.11 frame

Re-check after any edit:
    mkpsf.py > f.txt && text2pcap -l 105 f.txt f.pcap
    tshark -r f.pcap -V | grep -c Malformed        # must be 0

The body starts at awdl_hdr_t (type, version, sub_type, rsvd, phy_timestamp,
fw_timestamp -- 12 bytes). Category 127 and the 00:17:f2 OUI are supplied by the
firmware: awdl_af_hdr reads back ff:ff:ff:ff:ff:ff 7f 00 17 f2.

A bare 12-byte header parses as a valid header but a MALFORMED frame -- the
dissector wants the TLV chain -- so a header-only template would be dropped by
any real AWDL receiver.

Written from published field descriptions (Broadcom's wlioctl.h, the Wireshark
dissector, SEEMOO's papers), NOT from owl's GPLv3 source.
"""
import os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import awdl_identity as ident

# Our own AWDL address, read from the interface rather than baked in: it is
# derived from the machine's Wi-Fi MAC at awdl0 creation and differs on every
# machine. A wrong value here is not a cosmetic error -- it is the address a
# Mac builds its peer-cache entry from, and the one its unicast is sent to.
SELF = ident.mac_of(ident.AWDL_IFACE)
def tlv(t, body): return bytes([t]) + struct.pack('<H', len(body)) + body

# Real Apple traffic and macOS's own awdl_status both use encoding 1 (Legacy),
# two bytes per slot as {flags, channel}. Flags seen on air: 0x2b for a 2.4 GHz
# primary channel, 0x1d for a 5 GHz lower/40 MHz channel.
#
# CRITICAL for coexistence: the sequence must contain the INFRA channel, or the
# radio never returns to the AP and the STA association dies. macOS's live
# sequence during an AirDrop was  44 0 149 0 0 0 0 0 6 0 149 0 0 0 0 0  with its
# own infra channel (44) at slot 0, under strategy "Infra Priority".
CHFLAGS = {6: 0x2b, 44: 0x1d, 149: 0x1d, 151: 0x1d, 0: 0x00}

# The advertised sequence MUST be the one the firmware dwells on: a Mac
# schedules its unicast into the availability we advertise. awdl-up writes the
# firmware's from the same table and knob files, so they cannot drift.
from awdl_schedule import slots_for

def configured_slots():
    return slots_for(int(ident.knob('peer-chan', '44')),
                     int(ident.knob('master-chan', '6')),
                     ident.knob('chan-shape', 'dense'))

def chanseq(enc=1, chans=None, infra=None):
    if chans is None:
        chans = configured_slots()
        if infra:
            chans = [infra] + chans[1:]
    b = bytes([len(chans)-1, enc, 0, 3]) + struct.pack('<H', 0xffff)
    if enc == 0:
        return b + bytes(chans)
    return b + b''.join(bytes([CHFLAGS.get(c, 0x2b if c and c <= 14 else 0x1d), c])
                        for c in chans)

def sync_params():
    b  = struct.pack('<BHBB', 6, 0, 6, 0)          # next_aw_chan, tx_down_ctr, master_chan, guard
    b += struct.pack('<HHH', 16, 110, 0x1000)      # aw_period, af_period, flags (0x1000 on air, not owl's 0x1800)
    b += struct.pack('<HHH', 16, 16, 0)            # aw_ext, aw_com, remaining
    b += bytes([3,3,3,3])                          # min_ext, max_ext_*
    b += SELF                                      # master_addr
    b += bytes([4, 0])                             # presence_mode, reserved
    b += struct.pack('<HH', 0, 0)                  # next_aw_seq, ap_alignment
    return b + chanseq() + b'\x00\x00'             # inline chanseq + 2 pad

def election():
    # TLV 5, firmware-owned in template mode; built here only for the full-body
    # dissector check, from the same state as TLV 24 so the two never disagree.
    e = ELECTION
    return struct.pack('<BHBB', 0, 0, 0, 0) + e['master'] + struct.pack('<II', e['master_metric'], e['self_metric']) + b'\x00\x00'

# Election Parameters v2 (TLV 24): master_addr, sync_addr, master_counter,
# distance, master_metric, self_metric, unknown, reserved, self_counter -- the
# field the Macs actually read. The firmware owns TLV 5 and fills it live; TLV 24
# comes from the template, so the host has to keep it in step with the firmware's
# election state or the PSF contradicts itself. An early build sent master =
# self, metric 510, distance 0 here while TLV 5 said master = <a peer>, self
# metric 0, distance 1 -- a node claiming to be a top master at the Mac's own
# metric. Apple's driver rewrites the template on every election change.
ELECTION = dict(master=SELF, sync=SELF, distance=0, master_metric=0, self_metric=0)

def election_v2():
    e = ELECTION
    return e['master'] + e['sync'] + struct.pack('<IIIIIII', 0, e['distance'],
                                                 e['master_metric'], e['self_metric'], 0, 0, 0)

def service():   return b'\x00'*3 + struct.pack('<HI', 0, 0)
# Variant E6: a Mac's Service Parameters TLV instead of our empty one. Bonjour
# over AWDL is gated at the AWDL layer: a browser advertises a bloom filter of
# the services it seeks in TLV 6, and a peer's mDNSResponder (D2D plugin) only
# engages for a service whose bits it has seen. Measured: our PTR query for
# _airdrop._tcp reached a peer Mac's host and its mDNSResponder never logged or
# answered it, while that Mac still Discovered us from our pushed
# announcements. The bytes are a Mac's own MIF TLV 6 captured while its share
# sheet was open: sui 136, values 01 20 02 04 41. An idle Mac shares bits
# 4/5/7/19 with values 01/20/02/41 and lacks bit 13 (04); the share-sheet
# transition frames carried only bits 13 and 19 (04, 40) -- so bit 13 and bit 6
# of byte 19 are the AirDrop-browsing marks, the rest a Mac's standing services.
def service_e6(): return bytes.fromhex('0000008800b02008000120020441')
def version():   return bytes([0x10, 0x08])

# Variant D's Data Path State: 15 B, flags 0x8f24, social map 0x0001 (6 only).
def datapath_d(): return struct.pack('<H', 0x8f24) + b'X0\x00' + struct.pack('<H', 0x0001) + SELF + struct.pack('<H', 0)

# Variant E: the TLVs a Mac's PSF carries that D omitted, taken byte for byte
# from a real Apple PSF with the sender's MAC replaced by ours. Why: a peer Mac
# received every announcement we put on air and sent 240 TCP SYNs back, and not
# one arrived. The Mac's driver builds its firmware peer-cache entry for us from
# OUR PSF, and setPEER_CACHE_CONTROL requires the peer's HT Capabilities IE
# (TLV 7); without it there may be no entry, and unicast to a peer with no entry
# is dropped in the sender's firmware -- exactly what our own firmware does.
# TLV 12 is the Mac's 47-byte Data Path State (flags 0x239f, social map 0x0007,
# unicast options) instead of our 15 B / 0x0001. 17 = VHT caps, 33 = undecoded
# but present in every Mac PSF.
#
# The reference sender's address is not in the blob: it is split either side of
# the field and our own infrastructure MAC is spliced in, so nothing of the
# captured device's identity ships here.
INFRA = ident.infra_mac()   # the dissector reads this field as Infrastructure Address
_REF_HEAD = bytes.fromhex('239f43410007000000000000000000')
_REF_TAIL = bytes.fromhex('0400000000007d190000320e00000000000048720000f6240000')
def datapath_e():
    return _REF_HEAD + INFRA + _REF_TAIL
def htcap():     return bytes.fromhex('00006f881bffff00000000000000009600010000')
def vhtcap():    return bytes.fromhex('bf0c76f0b113faff0000faff0020')
def tlv33():     return bytes.fromhex('0100000005860100000000000000')

# Variant ES: E plus the AWDL-native service advertisement every Mac carries in
# its MIFs and we have never sent: Arpa (16, our hostname) and Service Response
# (2) records for _airdrop._tcp -- PTR, SRV, TXT -- in the exact layout of a
# Mac's MIF:
#   TLV 2 body: name_len(u16, counts the type byte) | name | type | data_len(u16)
#               | 0000 | rdata.  Names use AWDL's fixed dictionary: c0 07 =
#               _airdrop._tcp.local, c0 0a = _tcp.local, c0 0c = local,
#               c0 00 = end.  SRV rdata: prio, weight, port (big-endian) + target.
#   TLV 16 body: 03 | hostname label | c0 0c.
# Why: a Mac's AirDrop advert is here, not in mDNS (PTR _airdrop._tcp ->
# <service id>, SRV port 8770 -> <uuid>.local, TXT flags); a Mac's D2D layer
# confirms peers through these frames, and a peer that never sends them is
# dropped 0.5-3 min into every hold and never gets its mDNS queries answered.
# Same identity as the announcer: <awdl MAC>._airdrop._tcp, <host>.local, 8771.
HOST = ident.awdl_host().encode(); PORT = 8771; FLAGS = b'flags=136'
def _lbl(s): return bytes([len(s)]) + s
def _sr(name, rtype, rdata):
    return struct.pack('<H', len(name) + 1) + name + bytes([rtype]) + struct.pack('<H', len(rdata)) + b'\x00\x00' + rdata
def arpa():      return b'\x03' + _lbl(HOST) + b'\xc0\x0c'
def sr_ptr():    return _sr(b'\xc0\x07', 12, _lbl(SELF.hex().encode()) + b'\xc0\x00')
def sr_srv():    return _sr(_lbl(SELF.hex().encode()) + b'\xc0\x07', 33, struct.pack('>HHH', 0, 0, PORT) + _lbl(HOST) + b'\xc0\x0c')
def sr_txt():    return _sr(_lbl(SELF.hex().encode()) + b'\xc0\x07', 16, _lbl(FLAGS))

VARIANT = 'E'
def psf(skip=()):
    af  = struct.pack('<BBBBII', 8, 0x10, 0, 0, 0, 0)
    # Real on-air TLV order is 4, 18, 5, 6, 24, 12, 7, 17, 21, 33 -- note 18
    # comes SECOND. Variant D stops at 6, 24, 12(15 B), 21; variant E follows the
    if VARIANT == 'D':
        order = ((4, sync_params), (18, lambda: chanseq()+b'\x00'*3), (5, election),
                 (6, service), (24, election_v2), (12, datapath_d), (21, version))
    elif VARIANT == 'D7':
        # E parked the data path the moment it was written (015011Z: gate PROMPT,
        # template, then held 42 -> 489 and rx 0 for the hold). Bisect: D plus
        # the one TLV the peer cache needs.
        order = ((4, sync_params), (18, lambda: chanseq()+b'\x00'*3), (5, election),
                 (6, service), (24, election_v2), (12, datapath_d), (7, htcap), (21, version))
    else:
        order = ((4, sync_params), (18, lambda: chanseq()+b'\x00'*3), (5, election),
                 (6, service_e6 if VARIANT == 'E6' else service), (24, election_v2), (12, datapath_e), (7, htcap),
                 (17, vhtcap), (21, version), (33, tlv33))
        if VARIANT == 'ES':
            # Where a Mac's MIF puts them: after 21, before the 0-length trailers.
            order = order[:-2] + ((16, arpa), (2, sr_ptr), (2, sr_srv), (2, sr_txt), (21, version), (33, tlv33))
    for t, f in order:
        if t in skip: continue
        af += tlv(t, f())
    return af
if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    if '--service-only' in args:
        # Just the Arpa + Service Response TLVs (the ES additions), for awdl_afs_pload.
        print((tlv(16, arpa()) + tlv(2, sr_ptr()) + tlv(2, sr_srv()) + tlv(2, sr_txt())).hex()); raise SystemExit
    def opt(name, conv):
        if name in args:
            ELECTION[name[2:].replace('-', '_')] = conv(args[args.index(name) + 1])
    # --master aa:bb:cc:dd:ee:ff  the top master (etree top_master)
    # --sync   aa:bb:cc:dd:ee:ff  the node we sync to (opmode SYNCED TO); defaults to --master
    # --distance N                our distance to the top master (etree tree_depth)
    # --master-metric N           the top master's metric (etree)
    # --self-metric N             our own metric (etree self_metric)
    mac = lambda s: bytes.fromhex(s.replace(':', ''))
    opt('--master', mac); opt('--sync', mac)
    opt('--distance', int); opt('--master-metric', int); opt('--self-metric', int)
    if '--master' in args and '--sync' not in args:
        ELECTION['sync'] = ELECTION['master']
    # --variant D|E   D = TLVs 6 24 12(15 B) 21 (043300Z); E = Mac-shaped, adds 7 17 33 and the 47 B TLV 12. Default E.
    if '--variant' in args:
        VARIANT = args[args.index('--variant') + 1].upper()
    body = psf()
    if '--template' in sys.argv:
        # The awdl_payload template. On air (results/pcaps/tmplC-ch6-awdl.pcap,
        # 040243Z) the firmware emits its own awdl_hdr_t and TLVs 4, 5, 18, then
        # appends the template verbatim. A template that starts with awdl_hdr_t
        # is parsed by peers as TLV 8/len 16 + garbage -> Malformed. So: TLVs
        # only, and none of the three the firmware owns.
        body = psf(skip=(4, 5, 18))[12:]
    elif '--no-fw-tlvs' in sys.argv:
        # Same TLVs with the 12-byte header still in front. Kept for the
        # record; it is what 030025Z/040243Z transmitted and it is malformed.
        body = psf(skip=(4, 5, 18))
    if '--body' in sys.argv:
        print(body.hex()); raise SystemExit
    h  = struct.pack('<HH', 0x00d0, 0) + b'\xff'*6 + SELF + bytes.fromhex('002500ff9473') + struct.pack('<H', 0)
    frame = h + bytes([127]) + bytes.fromhex('0017f2') + body
    print('0000  ' + ' '.join(f'{b:02x}' for b in frame))
