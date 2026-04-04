"""
Photo CD Filesystem Reader — Phase 2 (v2)
==========================================
Mounts the ISO 9660 filesystem inside Track 01 of a Photo CD disc image
and lists all .PCD image files, reading metadata from INFO.PCD.

This version uses a direct ISO 9660 parser instead of pycdlib, which
avoids the "duplicate name" error caused by CD-ROM XA's dual directory
entry structure (Rock Ridge + plain ISO 9660 entries on the same disc).

Based on: System Description Photo CD, Philips/Kodak, January 1992
  - Section III.2.2: Directory Structure
  - Section III.2.3: INFO.PCD File
  ISO 9660 spec: ECMA-119

Photo CD directory structure (Section III.2.2, Fig. III.1):
  /
  ├── PHOTO_CD/
  │   ├── INFO.PCD        — disc/session metadata
  │   ├── PLAYLIST.PCD    — playback order + audio associations
  │   └── IMAGES/
  │       ├── IMG0001.PCD
  │       └── ...
  └── (CD-I application files)

Usage:
    python photocd_fs_reader.py "disc.cue"
"""

import sys
import os
import io
import struct
import argparse

from photocd_disc_map import (
    parse_cue, map_disc, validate_bin_files,
    SECTOR_SIZE_RAW, SECTOR_DATA_XA
)


# ---------------------------------------------------------------------------
# ISO 9660 CONSTANTS (ECMA-119)
# ---------------------------------------------------------------------------

ISO_SECTOR_SIZE        = 2048
ISO_PVD_SECTOR         = 16       # Primary Volume Descriptor is always at sector 16
ISO_VD_TYPE_PVD        = 1
ISO_VD_TYPE_TERMINATOR = 255


# ---------------------------------------------------------------------------
# TRACK 01 SECTOR READER
# ---------------------------------------------------------------------------

def _sector_skip(track_type):
    """Return the byte offset to user data for a given track type string."""
    if "MODE1" in track_type:
        return 16  # 12 sync + 4 header
    return 24      # 12 sync + 4 header + 8 XA subheader (MODE2/2352)


class DataTrackReader:
    """
    Random-access reader for a MODE1/2352 or MODE2/2352 data track .bin file.
    Strips the sync/header prefix from each sector to expose 2048-byte user data.

    For multi-bin: offset_lba=0 (the .bin starts at the track's first sector).
    For single-bin: offset_lba = track's index_01 LBA.
    """

    def __init__(self, bin_path, offset_lba=0, track_type="MODE2/2352"):
        self.bin_path   = bin_path
        self.offset_lba = offset_lba
        self._skip      = _sector_skip(track_type)
        self._file      = open(bin_path, "rb")

    def close(self):
        self._file.close()

    def read_sector(self, lba):
        """Read one 2048-byte user-data sector at the given absolute LBA."""
        local_lba   = lba - self.offset_lba
        byte_offset = local_lba * SECTOR_SIZE_RAW
        self._file.seek(byte_offset)
        raw = self._file.read(SECTOR_SIZE_RAW)
        if len(raw) < SECTOR_SIZE_RAW:
            raise IOError(f"Short read at LBA {lba} (local {local_lba})")
        return raw[self._skip : self._skip + ISO_SECTOR_SIZE]

    def read_file(self, lba, size):
        """Read an entire file by LBA + byte size."""
        buf = bytearray()
        sectors_needed = (size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
        for s in range(sectors_needed):
            buf.extend(self.read_sector(lba + s))
        return bytes(buf[:size])


class MultiTrackReader:
    """
    Reads across multiple consecutive MODE2/2352 data track bin files that form
    a single ISO 9660 volume.

    Needed for discs where Track 01 is a short stub (e.g. a CD-i application
    track) and the actual Photo CD image data spills into Track 02+.

    Volume LBA mapping
    ------------------
    For each data track i (in disc order, starting from the PVD track):

      vol_start[0] = 0
      vol_start[i] = vol_start[i-1] + (tracks[i-1].duration - tracks[i-1].index_01)

    The pregap (sectors 0..index_01-1 of each bin) is not part of the volume.
    To convert a volume LBA to a bin-local sector:

      local_lba = vol_lba - vol_start[i] + index_01[i]
                = vol_lba - (vol_start[i] - index_01[i])
                = vol_lba - offset_lba[i]

    where offset_lba[i] = vol_start[i] - index_01[i].
    """

    def __init__(self, data_tracks):
        """
        data_tracks : list of track dicts (from map_disc) in disc order,
                      starting from the track that contains the PVD.
                      Each dict must have: bin_file, index_01, duration.
        """
        self._spans = []   # list of (vol_start, vol_end, file_obj, offset_lba, skip)
        vol_start = 0
        for t in data_tracks:
            duration = t.get("duration") or 0
            # For CD-ROM XA data tracks the entire bin (from index_00 onward)
            # is part of the volume address space — even the "pregap" sectors
            # before index_01 are valid data sectors in the ISO 9660 volume.
            # Use index_00 as bin_start (usually 0); fall back to 0 if absent.
            bin_start    = t.get("index_00") if t.get("index_00") is not None else 0
            data_sectors = max(0, duration - bin_start)
            if data_sectors == 0:
                continue
            offset_lba = vol_start - bin_start
            vol_end    = vol_start + data_sectors
            skip       = _sector_skip(t.get("type", "MODE2/2352"))
            self._spans.append((vol_start, vol_end,
                                 open(t["bin_file"], "rb"), offset_lba, skip))
            vol_start = vol_end

        if not self._spans:
            raise ValueError("MultiTrackReader: no usable data track spans")

    def close(self):
        for _, _, f, _, _ in self._spans:
            f.close()

    def read_sector(self, lba):
        for vol_start, vol_end, fobj, offset_lba, skip in self._spans:
            if vol_start <= lba < vol_end:
                local_lba   = lba - offset_lba
                byte_offset = local_lba * SECTOR_SIZE_RAW
                fobj.seek(byte_offset)
                raw = fobj.read(SECTOR_SIZE_RAW)
                if len(raw) < SECTOR_SIZE_RAW:
                    raise IOError(f"Short read at LBA {lba} (local {local_lba})")
                return raw[skip : skip + ISO_SECTOR_SIZE]
        raise IOError(f"LBA {lba} not covered by any data track span")

    def read_file(self, lba, size):
        buf = bytearray()
        sectors_needed = (size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
        for s in range(sectors_needed):
            buf.extend(self.read_sector(lba + s))
        return bytes(buf[:size])


# ---------------------------------------------------------------------------
# ISO 9660 DIRECTORY PARSER (ECMA-119)
# ---------------------------------------------------------------------------

def _parse_dir_record(data, pos):
    """
    Parse one ISO 9660 directory record at `pos` within `data`.
    Returns dict or None if record_len == 0 (sector padding).

    Directory record layout (ECMA-119 section 9.1):
      Byte   0:     Record length
      Byte   1:     Extended attribute record length
      Bytes  2-9:   Extent LBA (both-endian 32-bit; we use little-endian at 2)
      Bytes 10-17:  Data length (both-endian 32-bit; little-endian at 10)
      Bytes 18-23:  Recording date/time (6 bytes)
      Byte  24:     File flags (bit 1 = directory)
      Byte  25:     File unit size
      Byte  26:     Interleave gap size
      Bytes 27-30:  Volume sequence number (both-endian 16-bit)
      Byte  31:     File identifier length (LEN_FI)
      Bytes 32+:    File identifier
    """
    if pos >= len(data):
        return None
    record_len = data[pos]
    if record_len == 0:
        return None
    if pos + record_len > len(data):
        return None

    rec  = data[pos : pos + record_len]
    lba  = struct.unpack_from("<I", rec, 2)[0]
    size = struct.unpack_from("<I", rec, 10)[0]
    flags  = rec[24]
    # Standard ISO 9660: bit 1 of flags = directory.
    # CD-ROM XA discs (including Photo CD) sometimes don't set this bit
    # even for genuine directories. We record it but don't rely on it
    # exclusively — find_entry() will attempt directory traversal regardless.
    is_dir = bool(flags & 0x02)
    # fi_len location: ISO 9660 spec says byte 31, but CD-ROM XA discs
    # (including Photo CD) place fi_len at byte 32 and the name at byte 33
    # due to an extra system-use byte. We try offset 32 first; fall back to 31.
    fi_len_32 = rec[32] if len(rec) > 32 else 0
    fi_len_31 = rec[31] if len(rec) > 31 else 0

    if fi_len_32 > 0 and 33 + fi_len_32 <= record_len:
        fi_len = fi_len_32
        fi     = rec[33 : 33 + fi_len]
    else:
        fi_len = fi_len_31
        fi     = rec[32 : 32 + fi_len]

    if fi in (b"\x00", b"\x00\x00"):
        name = "."
        is_dir = True
    elif fi in (b"\x01", b"\x01\x01"):
        name = ".."
        is_dir = True
    else:
        name = fi.decode("ascii", errors="replace")
        if ";" in name:
            name = name[:name.index(";")]
        if not is_dir and "." not in name:
            is_dir = True

    return {"name": name, "lba": lba, "size": size, "is_dir": is_dir}


def list_directory(reader, dir_lba, dir_size):
    """
    List all entries in an ISO 9660 directory, reading as many sectors
    as dir_size requires. Deduplicates entries by uppercase name so
    XA/Rock Ridge duplicate records don't appear twice.

    Returns list of record dicts (excluding . and ..)
    """
    entries    = []
    seen_names = set()
    sectors    = (dir_size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE

    for s in range(sectors):
        sector = reader.read_sector(dir_lba + s)
        pos = 0
        while pos < ISO_SECTOR_SIZE:
            rec = _parse_dir_record(sector, pos)
            if rec is None:
                break                      # padding zeros, rest of sector unused
            pos += sector[pos]             # advance by record_len

            if rec["name"] in (".", ".."):
                continue

            name_key = rec["name"].upper()
            if name_key in seen_names:
                continue                   # skip XA duplicate
            seen_names.add(name_key)
            entries.append(rec)

    return entries


def find_entry(reader, dir_lba, dir_size, target_name):
    """Case-insensitive search for a named entry in a directory."""
    for entry in list_directory(reader, dir_lba, dir_size):
        if entry["name"].upper() == target_name.upper():
            return entry
    return None


# ---------------------------------------------------------------------------
# PRIMARY VOLUME DESCRIPTOR (ECMA-119 section 8.4)
# ---------------------------------------------------------------------------

def read_pvd(reader):
    """
    Read the PVD at sector 16 and return root directory info.

    Supports both ISO 9660 ("CD001" at bytes 1-5) and High Sierra Group
    ("CDROM" at bytes 9-13).  The returned dict includes 'disc_format':
    either 'ISO9660' or 'HighSierra'.

    High Sierra differences from ISO 9660:
      - Standard identifier "CDROM" at bytes 9-13 (vs "CD001" at bytes 1-5)
      - Root directory record embedded at VD offset 180 (vs 156)
      - Directory records: 7-byte date/time field (vs 6), so file flags
        are at byte 25, fi_len at byte 32, fi at byte 33.
        (_parse_dir_record already handles this — it tries byte 32 first.)
    """
    sector = reader.read_sector(ISO_PVD_SECTOR)

    # --- ISO 9660 ---
    if sector[1:6] == b"CD001":
        vd_type = sector[0]
        if vd_type != ISO_VD_TYPE_PVD:
            raise ValueError(f"Sector 16 is not a PVD (type={vd_type})")
        volume_id = sector[40:72].decode("ascii", errors="replace").strip()
        root_rec  = sector[156:190]
        root_lba  = struct.unpack_from("<I", root_rec, 2)[0]
        root_size = struct.unpack_from("<I", root_rec, 10)[0]
        return {"volume_id": volume_id, "root_lba": root_lba, "root_size": root_size,
                "disc_format": "ISO9660"}

    # --- High Sierra Group ---
    if sector[9:14] == b"CDROM":
        volume_id = sector[40:72].decode("ascii", errors="replace").strip()
        # Root directory record at VD offset 180 in High Sierra
        root_rec  = sector[180:214]
        root_lba  = struct.unpack_from("<I", root_rec, 2)[0]
        root_size = struct.unpack_from("<I", root_rec, 10)[0]
        return {"volume_id": volume_id, "root_lba": root_lba, "root_size": root_size,
                "disc_format": "HighSierra"}

    raise ValueError(
        f"Not ISO 9660 or High Sierra "
        f"(bytes 1-5: {sector[1:6]!r}, bytes 9-14: {sector[9:14]!r})"
    )


# ---------------------------------------------------------------------------
# INFO.PCD PARSER (Spec Section III.2.3)
# ---------------------------------------------------------------------------

INFO_PCD_MAGIC = b"PCD_IPI\x00"


def parse_info_pcd(data):
    """
    Parse INFO.PCD binary content.
    Returns dict: disc_id, spec_version, session_count, image_count, valid.
    """
    result = {
        "disc_id": "?", "spec_version": "?",
        "session_count": 0, "image_count": 0, "valid": False,
    }
    if len(data) < 16:
        return result

    magic = data[0:8]
    if magic != INFO_PCD_MAGIC:
        print(f"    WARNING: INFO.PCD magic: {magic!r}")

    result["disc_id"]      = magic.decode("ascii", errors="replace").rstrip("\x00")
    result["spec_version"] = f"{data[8]}.{data[9]}"

    # Session count: big-endian uint16 at offset 12 (spec Section I.2)
    result["session_count"] = struct.unpack_from(">H", data, 12)[0]

    # Walk session descriptors for image counts (Fig. III.3)
    # Each descriptor: 246 bytes; image count uint16 BE at offset +10
    SESSION_OFFSET = 14
    SESSION_SIZE   = 246
    total = 0
    for s in range(max(1, result["session_count"])):
        off = SESSION_OFFSET + s * SESSION_SIZE
        if off + 12 > len(data):
            break
        total += struct.unpack_from(">H", data, off + 10)[0]

    result["image_count"] = total
    result["valid"]       = True
    return result


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def read_photo_cd_filesystem(cue_path):
    """
    Open the disc image, walk the ISO 9660 filesystem, and return
    (info_dict, images_list) where images_list is:
        [{"name": str, "lba": int, "size": int}, ...]
    """
    tracks, data_track, audio_tracks = map_disc(cue_path)
    if not data_track:
        print("ERROR: No data track found.")
        return None, []

    if not validate_bin_files(tracks):
        return None, []

    # Multi-bin: the track's .bin starts at local sector 0.
    # Single-bin: must offset by the track's start LBA.
    is_multi_bin = True
    for t in tracks:
        if t["number"] < data_track["number"] and t["bin_file"] == data_track["bin_file"]:
            is_multi_bin = False
            break

    offset_lba = 0 if is_multi_bin else (data_track["index_01"] or 0)
    print(f"  Track 01: {'multi-bin' if is_multi_bin else 'single-bin'}, "
          f"offset LBA={offset_lba}")

    reader = DataTrackReader(data_track["bin_file"], offset_lba=offset_lba,
                             track_type=data_track.get("type", "MODE2/2352"))

    try:
        pvd = read_pvd(reader)
    except Exception as e:
        print(f"  ERROR reading PVD: {e}")
        reader.close()
        return None, []

    print(f"  Volume ID : {pvd['volume_id']}")
    print(f"  Root dir  : LBA {pvd['root_lba']}, {pvd['root_size']} bytes\n")

    root_lba  = pvd["root_lba"]
    root_size = pvd["root_size"]

    # Navigate to PHOTO_CD/
    photo_cd = find_entry(reader, root_lba, root_size, "PHOTO_CD")
    if not photo_cd:
        print("  ERROR: PHOTO_CD/ not found in root directory.")
        reader.close()
        return None, []

    # Read INFO.PCD
    info = {"valid": False, "image_count": 0, "session_count": 0, "disc_id": "?"}
    info_entry = find_entry(reader, photo_cd["lba"], photo_cd["size"], "INFO.PCD")
    if info_entry:
        try:
            data = reader.read_file(info_entry["lba"], info_entry["size"])
            info = parse_info_pcd(data)
            print(f"  INFO.PCD:")
            print(f"    Disc ID      : {info['disc_id']}")
            print(f"    Spec version : {info['spec_version']}")
            print(f"    Sessions     : {info['session_count']}")
            print(f"    Images       : {info['image_count']}")
        except Exception as e:
            print(f"  WARNING: Could not parse INFO.PCD: {e}")
    else:
        print("  WARNING: INFO.PCD not found.")

    # Navigate to PHOTO_CD/IMAGES/
    images_dir = find_entry(reader, photo_cd["lba"], photo_cd["size"], "IMAGES")
    if not images_dir:
        print("  ERROR: PHOTO_CD/IMAGES/ not found.")
        reader.close()
        return info, []

    all_entries = list_directory(reader, images_dir["lba"], images_dir["size"])
    images = [
        {"name": e["name"], "lba": e["lba"], "size": e["size"]}
        for e in all_entries
        if e["name"].upper().endswith(".PCD")
    ]
    images.sort(key=lambda x: x["name"])

    print(f"\n  Found {len(images)} image(s) in PHOTO_CD/IMAGES/:\n")
    for i, img in enumerate(images, 1):
        kb = img["size"] / 1024
        print(f"    {i:>4}.  {img['name']:<16}  {kb:>8.1f} KB  (LBA {img['lba']})")

    print(f"\n  Audio: {len(audio_tracks)} Redbook CD-DA track(s)")
    for t in audio_tracks:
        secs = (t["duration"] or 0) / 75
        m, s = int(secs // 60), int(secs % 60)
        print(f"    Track {t['number']:02d}: {m}:{s:02d}  —  "
              f"{os.path.basename(t['bin_file'])}")

    reader.close()
    return info, images


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Photo CD Filesystem Reader — Phase 2")
    parser.add_argument("cue", help="Path to .cue file")
    args = parser.parse_args()

    if not os.path.isfile(args.cue):
        print(f"Error: not found: {args.cue}")
        sys.exit(1)

    info, images = read_photo_cd_filesystem(args.cue)
    print(f"\n  {'Ready for Phase 3 — image decoding.' if images else 'No images found.'}")
