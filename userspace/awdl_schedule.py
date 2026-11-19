"""The AWDL channel sequence: one table for the firmware's dwell and our PSF.

awdl-up writes the firmware's `awdl_chan_seq` from this table and mkpsf.py
advertises the same slots in TLV 18. A Mac schedules its unicast into the
availability we advertise, so the two must never differ.

  awdl_schedule.py check PEER MASTER SHAPE [INFRA]   exit 0, or a reason and exit 2
  awdl_schedule.py firmware PEER MASTER SHAPE        the awdl_chan_seq hex
  awdl_schedule.py master [INFRA [PEER SHAPE]]       the master channel when none is configured
  awdl_schedule.py peer MASTER SHAPE [INFRA]         the peer channel when none is configured
  awdl_schedule.py shape [INFRA]                     the shape when none is configured
"""
import struct
import sys

# A Mac's live shape observed on air is `mirror` (ten nonzero slots). Macs
# re-derive their sequence over time, so it is a moving target.
SHAPES = {
    'sparse': lambda p, m: [p, 0, 149, 0, 0, 0, 0, 0, m, 0, 149, 0, 0, 0, 0, 0],
    'dense': lambda p, m: [p, p, 149, 0, 0, 0, 0, p, m, p, 149, p, 0, 0, 0, p],
    'full': lambda p, m: [p, p, 149, p, p, 149, p, p, m, p, 149, p, p, 149, p, p],
    'dense44': lambda p, m: [p, p, 149, p, p, p, p, p, m, p, 149, p, p, p, p, p],
    'mirror': lambda p, m: [p, 0, 149, 0, 0, 149, p, p, m, 0, 149, 0, 0, 149, p, p],
    # A Mac on 6 GHz Wi-Fi advertises 149 at 0-3 and 9-11 (#79).
    'mac6g': lambda p, m: [149, 149, 149, 149, p, 0, 0, p, m, 149, 149, 149, p, 0, 0, p],
}

# Operating class per channel, as the firmware's encoding 3 carries it.
OPCLASS = {0: 0, 1: 81, 6: 81, 11: 81, 36: 128, 40: 128, 44: 128, 48: 128,
           149: 128, 153: 128, 157: 128, 161: 128}


def band(ch):
    """'2.4' or '5' for a channel in this table, else None; awdl-up's frequency mapping keeps 6 GHz out."""
    return {81: '2.4', 128: '5'}.get(OPCLASS.get(ch))


def default_master(infra, peer=None, shape=None):
    """149 while the Wi-Fi is on a 5 GHz channel in this table, else 6 as before.

    Given the peer channel and shape, a 5 GHz default whose sequence would
    never visit the Wi-Fi channel gives way to the Wi-Fi channel itself: Wi-Fi
    on 157 or 36 with peer 44 gets that channel as master rather than a 149
    schedule that check refuses. 2.4 GHz is left to default_peer, which moves
    the peer slots to channel 1 or 11. Every default that already passed is kept.
    """
    mch = 149 if band(infra) == '5' else 6
    if (band(infra) == '5' and peer is not None and shape in SHAPES
            and infra not in slots_for(peer, mch, shape)):
        return infra
    return mch


def default_shape(infra):
    """mac6g while the Wi-Fi is on 44, the channel it was measured on (#79); else dense as before."""
    return 'mac6g' if infra == 44 else 'dense'


def slots_for(peer, mch, shape):
    return SHAPES[shape](peer, mch)


def default_peer(infra, mch, shape):
    """44 as before, unless that leaves Wi-Fi channel 1 or 11 unvisited: then that channel, as a Mac's own takes these slots."""
    if infra in (1, 11) and shape in SHAPES and infra not in slots_for(44, mch, shape):
        return infra
    return 44


def problem(peer, mch, shape, infra=None):
    """Why this schedule must not be written, or None."""
    if shape not in SHAPES:
        return f'unknown chan-shape {shape!r}; known: {", ".join(sorted(SHAPES))}'
    slots = slots_for(peer, mch, shape)
    missing = sorted({c for c in slots if c not in OPCLASS})
    if missing:
        return f'no operating class for channel(s) {missing}'
    # mkpsf.py: without the infrastructure channel the radio never returns to the AP.
    if infra and infra not in slots:
        return f'the sequence {slots} never visits the Wi-Fi channel {infra}; the association would drop'
    return None


def firmware_chanseq(slots):
    """`awdl_chan_seq` for the firmware: encoding 3, {channel, operating class} per slot."""
    return (bytes([len(slots) - 1, 3, 0, 3]) + struct.pack('<H', 0xffff)
            + b''.join(bytes([c, OPCLASS[c]]) for c in slots))


def main(argv):
    if (argv[:1] == ['master'] and len(argv) in (1, 2, 4)) or (argv[:1] == ['shape'] and len(argv) <= 2):
        try:
            infra = int(argv[1]) if len(argv) >= 2 and argv[1] else None
            peer = int(argv[2]) if len(argv) == 4 else None
        except ValueError:
            print('channels must be numbers', file=sys.stderr)
            return 2
        if argv[0] == 'shape':
            print(default_shape(infra))
        else:
            print(default_master(infra, peer, argv[3] if len(argv) == 4 else None))
        return 0
    if argv[:1] == ['peer'] and len(argv) in (3, 4):
        try:
            mch = int(argv[1])
            infra = int(argv[3]) if len(argv) == 4 and argv[3] else None
        except ValueError:
            print('channels must be numbers', file=sys.stderr)
            return 2
        print(default_peer(infra, mch, argv[2]))
        return 0
    if len(argv) < 4 or argv[0] not in ('check', 'firmware'):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        peer, mch = int(argv[1]), int(argv[2])
        infra = int(argv[4]) if argv[0] == 'check' and len(argv) > 4 and argv[4] else None
    except ValueError:
        print('channels must be numbers', file=sys.stderr)
        return 2
    why = problem(peer, mch, argv[3], infra)
    if why:
        print(why, file=sys.stderr)
        return 2
    if argv[0] == 'firmware':
        print(firmware_chanseq(slots_for(peer, mch, argv[3])).hex())
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
