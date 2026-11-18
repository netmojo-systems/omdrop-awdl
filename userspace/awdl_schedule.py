"""The AWDL channel sequence: one table for the firmware's dwell and our PSF.

awdl-up writes the firmware's `awdl_chan_seq` from this table and mkpsf.py
advertises the same slots in TLV 18. A Mac schedules its unicast into the
availability we advertise, so the two must never differ.

  awdl_schedule.py check PEER MASTER SHAPE [INFRA]   exit 0, or a reason and exit 2
  awdl_schedule.py firmware PEER MASTER SHAPE        the awdl_chan_seq hex
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
}

# Operating class per channel, as the firmware's encoding 3 carries it.
OPCLASS = {0: 0, 1: 81, 6: 81, 11: 81, 36: 128, 40: 128, 44: 128, 48: 128,
           149: 128, 153: 128, 157: 128, 161: 128}


def slots_for(peer, mch, shape):
    return SHAPES[shape](peer, mch)


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
