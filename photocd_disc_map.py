"""
Photo CD Disc Mapper — Phase 1
==============================
Parses a .cue sheet (single-bin OR multi-bin) and maps all tracks on the disc.

Based on: System Description Photo CD, Philips/Kodak, January 1992
  - Chapter II: General Disc Format
  - Section II.3.2.4: CD-DA Track(s)
  - Section III.2: File System / Directory Structure

Disc layout per spec (Section II.3, Fig. II.1):
  Track 01 — CD-ROM XA data track (MODE2/2352)
               Contains: ISO 9660 filesystem with PHOTO_CD/ directory tree
               Files: INFO.PCD, PLAYLIST.PCD, IMAGES/*.PCD
  Track 02+ — CD-DA audio tracks (Redbook PCM audio, 16-bit 44.1kHz stereo)

Multi-bin support:
  Some .cue files reference one .bin per track rather than one large .bin.
  Each TRACK directive may be preceded by its own FILE directive.
  This parser handles both cases transparently.

Usage:
    python photocd_disc_map.py your_disc.cue
"""

import sys
import os
import struct


# ---------------------------------------------------------------------------
# SECTOR CONSTANTS (per CD-ROM XA / Red Book spec)
# ---------------------------------------------------------------------------

SECTOR_SIZE_RAW     = 2352   # raw sector size in bytes (Mode 2 Form 1/2, CD-DA)
SECTOR_SYNC_SIZE    = 12     # sync bytes at start of each data sector
SECTOR_HEADER_SIZE  = 4      # header: minute, second, frame, mode
SECTOR_SUBHEADER    = 8      # CD-ROM XA subheader (2x repeated 4-byte subheader)
SECTOR_DATA_XA      = 2048   # usable data bytes in Mode 2 Form 1 sector
SECTOR_DATA_CDDA    = 2352   # CD-DA: entire sector is audio PCM

# LBA offset: Red Book defines track 1 to start at LBA 0,
# but the physical disc has a 2-second (150 frame) pre-gap.
# CUE INDEX 01 times are relative to LBA 0 = 00:02:00 MSF.
PREGAP_SECTORS = 150


# ---------------------------------------------------------------------------
# MSF / LBA CONVERSION
# ---------------------------------------------------------------------------

def msf_to_lba(msf_str):
    """
    Convert MM:SS:FF (minutes, seconds, frames) to Logical Block Address.
    LBA 0 corresponds to MSF 00:00:00 in the .cue file.
    Per Red Book: 75 frames per second, 60 seconds per minute.
    """
    parts = msf_str.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid MSF format: {msf_str!r}")
    mm, ss, ff = int(parts[0]), int(parts[1]), int(parts[2])
    return (mm * 60 + ss) * 75 + ff


def lba_to_msf(lba):
    """Convert LBA back to MM:SS:FF string."""
    ff = lba % 75
    ss = (lba // 75) % 60
    mm = lba // (75 * 60)
    return f"{mm:02d}:{ss:02d}:{ff:02d}"


# ---------------------------------------------------------------------------
# CUE TOKENIZER
# ---------------------------------------------------------------------------

def _tokenize(line):
    """
    Split a .cue line into tokens, respecting quoted strings.
    e.g. FILE "my disc track 01.bin" BINARY  →  ['FILE', 'my disc track 01.bin', 'BINARY']
    """
    tokens = []
    current = []
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch in (' ', '\t') and not in_quotes:
            if current:
                tokens.append(''.join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append(''.join(current))
    return tokens


# ---------------------------------------------------------------------------
# CUE PARSER — multi-bin aware
# ---------------------------------------------------------------------------

def parse_cue(cue_path):
    """
    Parse a .cue sheet and return a list of track dicts.

    Handles both:
      - Single-bin:  one FILE line at top, all TRACKs reference it
      - Multi-bin:   each TRACK preceded by its own FILE line

    Returns list of dicts:
        number    : int   — track number (1-based)
        type      : str   — 'MODE2/2352', 'AUDIO', etc.
        bin_file  : str   — absolute path to the .bin for this track
        index_00  : int   — pre-gap start LBA (or None)
        index_01  : int   — track start LBA (authoritative position)
        duration  : int   — sector count (filled after all tracks parsed)
    """
    cue_dir = os.path.dirname(os.path.abspath(cue_path))
    tracks = []
    current_track = None
    current_bin = None          # most recently seen FILE declaration

    with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    for raw_line in lines:
        line = raw_line.strip()
        tokens = _tokenize(line)
        if not tokens:
            continue
        cmd = tokens[0].upper()

        if cmd == "FILE":
            # Resolve path relative to .cue location
            fname = tokens[1] if len(tokens) > 1 else ""
            current_bin = os.path.join(cue_dir, fname)

        elif cmd == "TRACK":
            if current_track is not None:
                tracks.append(current_track)
            current_track = {
                "number":   int(tokens[1]) if len(tokens) > 1 else len(tracks) + 1,
                "type":     tokens[2].upper() if len(tokens) > 2 else "UNKNOWN",
                "bin_file": current_bin,   # snapshot of most recent FILE
                "index_00": None,
                "index_01": None,
                "duration": None,
            }

        elif cmd == "INDEX" and current_track is not None:
            idx_num = int(tokens[1]) if len(tokens) > 1 else -1
            lba     = msf_to_lba(tokens[2]) if len(tokens) > 2 else 0
            if idx_num == 0:
                current_track["index_00"] = lba
            elif idx_num == 1:
                current_track["index_01"] = lba

        elif cmd == "PREGAP" and current_track is not None:
            # PREGAP MM:SS:FF — virtual gap, no actual data in bin
            pass  # we note it but don't need it for Phase 1

        elif cmd == "POSTGAP" and current_track is not None:
            pass  # likewise

    if current_track is not None:
        tracks.append(current_track)

    # Calculate per-track durations (sector count).
    # For single-bin: subtract consecutive index_01 values.
    # For multi-bin:  each bin's size / SECTOR_SIZE_RAW gives the count.
    _calculate_durations(tracks)

    return tracks


def _calculate_durations(tracks):
    """
    Fill in the 'duration' field for each track (number of sectors).

    Multi-bin case: each track has its own file; use file size.
    Single-bin case: use the gap between consecutive index_01 LBAs;
                     last track uses file size as upper bound.
    """
    for i, track in enumerate(tracks):
        bin_path = track["bin_file"]
        is_multi_bin = (
            i == 0 or tracks[i]["bin_file"] != tracks[i - 1]["bin_file"]
        )

        if is_multi_bin and bin_path and os.path.isfile(bin_path):
            # Each .bin holds exactly the sectors for one track
            file_size = os.path.getsize(bin_path)
            track["duration"] = file_size // SECTOR_SIZE_RAW
        elif i + 1 < len(tracks):
            # Single-bin: next track starts where this one ends
            next_lba = tracks[i + 1]["index_01"]
            this_lba = track["index_01"]
            if next_lba is not None and this_lba is not None:
                track["duration"] = next_lba - this_lba
        else:
            # Last track in single-bin: derive from file size
            if bin_path and os.path.isfile(bin_path):
                file_size = os.path.getsize(bin_path)
                start_lba = track["index_01"] or 0
                total_sectors = file_size // SECTOR_SIZE_RAW
                track["duration"] = total_sectors - start_lba


# ---------------------------------------------------------------------------
# BIN SECTOR READER
# ---------------------------------------------------------------------------

def read_sector(bin_path, lba, track_type="MODE2/2352", offset_lba=0):
    """
    Read one raw sector from a .bin file at the given LBA.

    bin_path   : path to the .bin file
    lba        : absolute LBA on disc
    track_type : determines how to interpret the 2352-byte sector
    offset_lba : for multi-bin, the LBA where this bin's track begins
                 (subtract from lba to get byte offset into the file)

    Returns the 2048 bytes of user data for data sectors,
    or all 2352 bytes for CD-DA audio sectors.
    """
    local_lba = lba - offset_lba
    byte_offset = local_lba * SECTOR_SIZE_RAW

    with open(bin_path, "rb") as f:
        f.seek(byte_offset)
        raw = f.read(SECTOR_SIZE_RAW)

    if len(raw) < SECTOR_SIZE_RAW:
        raise IOError(f"Short read at LBA {lba} (local {local_lba}) in {bin_path}")

    if "AUDIO" in track_type:
        return raw  # full 2352 bytes of PCM audio

    if "MODE1" in track_type:
        # Mode 1 sector layout:
        # Bytes  0–11:  sync pattern
        # Bytes 12–15:  header (MSF + mode)
        # Bytes 16–2063: user data (2048 bytes)
        # Bytes 2064–2351: EDC/ECC
        return raw[16:16 + SECTOR_DATA_XA]

    # Mode 2 Form 1 (CD-ROM XA) sector layout:
    # Bytes  0–11:  sync pattern
    # Bytes 12–15:  header (MSF + mode)
    # Bytes 16–23:  XA subheader (8 bytes, 2x repeated 4-byte block)
    # Bytes 24–2071: user data (2048 bytes)
    # Bytes 2072–2351: EDC/ECC
    return raw[24:24 + SECTOR_DATA_XA]


# ---------------------------------------------------------------------------
# DISC MAPPER / REPORTER
# ---------------------------------------------------------------------------

def map_disc(cue_path):
    """
    Parse the .cue and print a human-readable disc map.
    Returns (tracks, data_track, audio_tracks) for use by subsequent phases.

    data_track is the MODE2 track whose bin file contains a valid ISO 9660
    filesystem with a PHOTO_CD/ directory. Per spec, Track 01 is always a
    CD-ROM XA data track, but some discs use Track 01 only as a short
    reference/stub and place the main filesystem on Track 02. We try each
    data track in order until we find one with a valid PVD.
    """
    print(f"\n{'='*60}")
    print(f"  Photo CD Disc Mapper")
    print(f"  Spec: System Description Photo CD, Philips/Kodak 1992")
    print(f"{'='*60}")
    print(f"  CUE: {cue_path}\n")

    tracks = parse_cue(cue_path)

    if not tracks:
        print("  ERROR: No tracks found. Is this a valid .cue file?")
        return None, None, []

    data_tracks  = []
    audio_tracks = []

    for t in tracks:
        num      = t["number"]
        ttype    = t["type"]
        lba      = t["index_01"]
        duration = t["duration"]
        bin_name = os.path.basename(t["bin_file"]) if t["bin_file"] else "(no file)"
        dur_sec  = f"{duration / 75:.1f}s" if duration else "?"

        tag = ""
        if "MODE" in ttype:
            tag = "  ← Photo CD data (ISO 9660 + PHOTO_CD/)"
            data_tracks.append(t)
        elif "AUDIO" in ttype:
            tag = "  ← Redbook CD-DA audio"
            audio_tracks.append(t)

        print(f"  Track {num:02d}  [{ttype:12s}]  "
              f"LBA {lba or 0:>6}  ({dur_sec:>8})  {bin_name}{tag}")

    n_data  = len(data_tracks)
    n_audio = len(audio_tracks)
    print()
    print(f"  Summary: {len(tracks)} track(s) total — "
          f"{n_data} data, {n_audio} audio")

    if not data_tracks:
        print("  WARNING: No data track found. Cannot read Photo CD images.")
        print()
        return tracks, None, audio_tracks

    if not audio_tracks:
        print("  NOTE: No CD-DA audio tracks found on this disc.")

    # Find the data track that contains a valid ISO 9660 filesystem.
    # Try each data track's bin file independently (multi-bin: offset_lba=0).
    data_track = _find_iso_data_track(data_tracks)

    if data_track is None:
        print("  WARNING: No data track with valid ISO 9660 filesystem found.")
        data_track = data_tracks[0]  # fall back to first data track
    elif len(data_tracks) > 1:
        print(f"  ISO 9660 filesystem found on Track {data_track['number']:02d}: "
              f"{os.path.basename(data_track['bin_file'])}")

    print()
    return tracks, data_track, audio_tracks


def _find_iso_data_track(data_tracks):
    """
    Try each data track's bin file and return the first one containing
    a valid ISO 9660 Primary Volume Descriptor.

    Multi-bin discs: each track's bin starts at local LBA 0.
    The PVD is always at LBA 16 in the filesystem.
    """
    ISO_PVD_LBA        = 16
    ISO_PVD_IDENTIFIER = b'CD001'

    for track in data_tracks:
        bin_path = track["bin_file"]
        if not bin_path or not os.path.isfile(bin_path):
            continue
        try:
            # In a multi-bin disc each bin starts at sector 0 locally.
            # Read sector 16 (PVD) with offset_lba=0.
            sector = read_sector(bin_path, ISO_PVD_LBA,
                                 track_type=track["type"], offset_lba=0)
            # ISO 9660 PVD: byte 1-5 = "CD001"
            if sector[1:6] == ISO_PVD_IDENTIFIER:
                return track
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# MULTI-BIN VALIDATOR
# ---------------------------------------------------------------------------

def validate_bin_files(tracks):
    """Check that every referenced .bin file actually exists and is readable."""
    seen = set()
    all_ok = True
    print("  Validating .bin file(s)...")
    for t in tracks:
        path = t["bin_file"]
        if path in seen:
            continue
        seen.add(path)
        if path is None:
            print(f"    [MISSING] Track {t['number']:02d}: no FILE reference in .cue")
            all_ok = False
        elif not os.path.isfile(path):
            print(f"    [MISSING] {os.path.basename(path)}")
            all_ok = False
        else:
            size = os.path.getsize(path)
            sectors = size // SECTOR_SIZE_RAW
            print(f"    [  OK   ] {os.path.basename(path)}  "
                  f"({size:,} bytes = {sectors} sectors)")
    if all_ok:
        print("    All .bin files present.\n")
    else:
        print("    Some .bin files are missing — cannot proceed.\n")
    return all_ok


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python photocd_disc_map.py <path/to/disc.cue>")
        sys.exit(1)

    cue_path = sys.argv[1]
    if not os.path.isfile(cue_path):
        print(f"Error: file not found: {cue_path}")
        sys.exit(1)

    tracks, data_track, audio_tracks = map_disc(cue_path)
    if tracks:
        validate_bin_files(tracks)
