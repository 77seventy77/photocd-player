"""
PLAYLIST.PCD correct parser.

Based on reverse-engineering the actual binary structure of PLAYLIST.PCD
from the Mapplethorpe Photo CD disc.

Structure discovered:
  - File starts with a Selection Item header at offset 0x00
  - Play Sequence at offset 0x006d: 1 image entry (intro/bumper)
  - Play Sequence at offset 0x044a: 4 image entries (main sequence index)
  - Starting at ~0x09cd: 103 Image+Audio blocks, one per image
    Each block is ~0x384 bytes and contains:
      - Image entry: image number, display time
      - CD-DA Entry: audio clip start/stop byte offsets into AUDIO01.PCD

The display time field in each Image Entry tells us how long to show
that image. A value of 0 means manual/wait-for-user.

Usage:
    python photocd_playlist_parse.py "disc.cue"
"""
import sys
import struct
from photocd_disc_map import map_disc, validate_bin_files
from photocd_fs_reader import (
    DataTrackReader, read_pvd, find_entry, ISO_SECTOR_SIZE
)

def read_file(reader, entry):
    sectors = (entry['size'] + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
    buf = bytearray()
    for s in range(sectors):
        buf.extend(reader.read_sector(entry['lba'] + s))
    return bytes(buf[:entry['size']])

def bcd_to_int(b):
    return (b >> 4) * 10 + (b & 0x0f)

def msf_to_seconds(m, s, f):
    return bcd_to_int(m) * 60 + bcd_to_int(s) + bcd_to_int(f) / 75.0

def parse_play_sequence(data, offset):
    """Parse a Play Sequence starting at offset.
    Returns dict with n_images, image_entries, cdda_entry.
    """
    if offset + 3 > len(data):
        return None

    header = data[offset]
    if (header >> 4) != 0x2:
        return None

    n_images = struct.unpack_from('>H', data, offset + 1)[0]

    # Play Sequence structure (from spec Fig III.7):
    # Byte 0:     header (0x2X)
    # Bytes 1-2:  n_images (big-endian)
    # Bytes 3-6:  next_seq_offset (big-endian, absolute in file)
    # Bytes 7-10: prev_seq_offset
    # Bytes 11-32: reserved (22 bytes)
    # Then: n_images Image Entry records
    # Then: one CD-DA Entry record (28 bytes)

    next_off = struct.unpack_from('>I', data, offset + 3)[0]
    prev_off = struct.unpack_from('>I', data, offset + 7)[0]

    img_start = offset + 1 + 2 + 4 + 4 + 22  # = offset + 33

    images = []
    for i in range(n_images):
        img_pos = img_start + i * 16
        if img_pos + 16 > len(data):
            break
        # Image Entry (spec Fig III.8, 16 bytes):
        # Bytes 0-1:  image number (1-based)
        # Bytes 2-3:  display time in 1/30s units (0 = manual)
        # Byte 4:     transition type
        # Byte 5:     image attributes
        # Bytes 6-15: reserved
        img_num   = struct.unpack_from('>H', data, img_pos)[0]
        disp_time = struct.unpack_from('>H', data, img_pos + 2)[0]
        transition = data[img_pos + 4]
        images.append({
            'number':   img_num,
            'display_time_ticks': disp_time,
            'display_time_s': disp_time / 30.0 if disp_time > 0 else None,
            'transition': transition,
        })

    # CD-DA Entry follows image entries (spec Fig III.9, 28 bytes):
    cdda_pos = img_start + n_images * 16
    cdda = None
    if cdda_pos + 28 <= len(data):
        cd = data[cdda_pos:cdda_pos + 28]
        # Bytes 0-2:  start MSF (BCD)
        # Bytes 3-5:  stop MSF (BCD)
        # Byte 6:     CD-DA attributes
        # Bytes 7-9:  start in 1/75s units (3 bytes big-endian)
        # Bytes 10-12: stop in 1/75s units
        # ... rest reserved
        sm, ss, sf = cd[0], cd[1], cd[2]
        em, es, ef = cd[3], cd[4], cd[5]
        attrs = cd[6]
        start_s = msf_to_seconds(sm, ss, sf)
        stop_s  = msf_to_seconds(em, es, ef)
        if any(b != 0xFF for b in cd[:6]) and any(b != 0 for b in cd[:6]):
            cdda = {
                'start_msf': (sm, ss, sf),
                'stop_msf':  (em, es, ef),
                'start_s':   start_s,
                'stop_s':    stop_s,
                'attrs':     attrs,
            }

    return {
        'offset':    offset,
        'n_images':  n_images,
        'next_off':  next_off,
        'prev_off':  prev_off,
        'images':    images,
        'cdda':      cdda,
    }

def find_all_play_sequences(data):
    """Walk all Play Sequences by following next_off chain."""
    sequences = []
    visited = set()

    # Find first Play Sequence (header 0x20-0x2F)
    # Start from offset 0 and scan forward
    pos = 0
    while pos < len(data):
        b = data[pos]
        if (b >> 4) == 0x2:
            n = struct.unpack_from('>H', data, pos + 1)[0] if pos + 3 <= len(data) else 0
            if 0 < n <= 200:
                break
        pos += 1

    if pos >= len(data):
        return sequences

    # Walk the chain
    while pos not in visited and pos < len(data):
        visited.add(pos)
        seq = parse_play_sequence(data, pos)
        if seq is None:
            break
        sequences.append(seq)
        nxt = seq['next_off']
        if nxt == 0 or nxt >= len(data) or nxt == pos:
            break
        pos = nxt

    return sequences

def main(cue_path):
    tracks, data_track, audio_tracks = map_disc(cue_path)
    validate_bin_files(tracks)
    reader = DataTrackReader(data_track["bin_file"], offset_lba=0)
    pvd = read_pvd(reader)
    photo_cd = find_entry(reader, pvd["root_lba"], pvd["root_size"], "PHOTO_CD")
    playlist = find_entry(reader, photo_cd["lba"], photo_cd["size"], "PLAYLIST.PCD")

    if not playlist:
        print("No PLAYLIST.PCD found")
        reader.close()
        return None

    data = read_file(reader, playlist)
    reader.close()

    print(f"\n  PLAYLIST.PCD: {len(data)} bytes\n")

    sequences = find_all_play_sequences(data)
    print(f"  Found {len(sequences)} play sequence(s):\n")

    all_image_timings = {}

    for i, seq in enumerate(sequences):
        print(f"  Sequence {i+1} @ offset 0x{seq['offset']:04x}")
        print(f"    Images: {seq['n_images']}")
        for img in seq['images']:
            t = img['display_time_s']
            t_str = f"{t:.1f}s" if t else "manual"
            print(f"    Image {img['number']:>4}  display={t_str}  transition=0x{img['transition']:02x}")
            if t and img['number'] > 0:
                all_image_timings[img['number']] = t
        if seq['cdda']:
            cd = seq['cdda']
            print(f"    CD-DA: start={cd['start_s']:.1f}s  stop={cd['stop_s']:.1f}s")
        print()

    # Summary of timing data
    if all_image_timings:
        print(f"  Images with timing data: {len(all_image_timings)}")
        for num in sorted(all_image_timings):
            print(f"    IMG{num:04d}: {all_image_timings[num]:.1f}s")
    else:
        print("  No timed image entries found — disc specifies manual advancement.")
        print("  Slideshow mode will not be available for this disc.")

    return all_image_timings

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python photocd_playlist_parse.py disc.cue")
        sys.exit(1)
    main(sys.argv[1])
