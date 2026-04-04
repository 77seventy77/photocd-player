"""
Photo CD Image Decoder — Phase 3
==================================
Decodes a .PCD image file from a Photo CD disc image and displays it
in a tkinter window at Base resolution (768 x 512 pixels).

Based on: System Description Photo CD, Philips/Kodak, January 1992
  - Chapter IV: Image Data Representations
  - Section IV.2.5: Color Encoding (Photo YCC)
  - Section IV.3: Image Packs
  - Section IV.3.2: Structure and entries of the Image Pack
  - Section IV.3.10/11: Base Image Component Attributes and Data

Image Pack structure (spec Section IV.3.2, Fig. IV.9):
  Each .PCD file is an Image Pack containing all 5 resolution tiers:
    Tier 1: Base/16  192 x 128    (stored directly, no compression)
    Tier 2: Base/4   384 x 256    (stored directly, no compression)
    Tier 3: Base     768 x 512    (stored directly, no compression) ← we decode this
    Tier 4: 4Base    1536 x 1024  (Huffman-coded residual)
    Tier 5: 16Base   3072 x 2048  (Huffman-coded residual)

  Each tier stores 3 components: Y (luminance), Cb, Cr (chrominance at half res)

Color encoding (spec Section IV.2.5 — Photo YCC):
  Photo CD uses a variant of the Kodak Photo YCC color space.
  Components are stored as unsigned 8-bit integers (0..255).

  Conversion to linear RGB (per spec):
    Y  component range: 0..255  (luminance)
    C1 component range: 0..255  (Cb, shifted: C1 = Cb + 156)
    C2 component range: 0..255  (Cr, shifted: C2 = Cr + 137)

  Photo YCC → sRGB conversion matrix (CCIR 709 primaries, spec Section IV.2.5):
    r = Y                        + 1.402   * (C2 - 156)   -- wait, see note below
  
  Actual Kodak Photo YCC to RGB (well-documented decode path):
    Yd  = Y  / 255.0
    C1d = (C1 - 156) / 255.0     (Cb centered around 0)
    C2d = (C2 - 137) / 255.0     (Cr centered around 0)

    r_linear = Yd                   + 1.630 * C2d
    g_linear = Yd - 0.194 * C1d    - 0.509 * C2d
    b_linear = Yd + 1.990 * C1d

  Then gamma-expand from Photo CD's 2.2 gamma and clamp to [0, 255].

Usage:
    python photocd_decoder.py "disc.cue" [image_number]
    python photocd_decoder.py "disc.cue" 1        # shows IMG0001.PCD
    python photocd_decoder.py "disc.cue" 42       # shows IMG0042.PCD
"""

import sys
import os
import json
import struct
import argparse
import tkinter as tk
from tkinter import ttk, filedialog

try:
    from PIL import Image, ImageTk
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)

try:
    import cairosvg as _cairosvg
    import io as _io
    _CAIROSVG_OK = True
except (ImportError, OSError):
    _CAIROSVG_OK = False

from photocd_disc_map import map_disc, validate_bin_files
from photocd_fs_reader import (
    DataTrackReader, MultiTrackReader, read_pvd, find_entry, list_directory, ISO_SECTOR_SIZE
)


# ---------------------------------------------------------------------------
# CONFIG — persisted user settings
# ---------------------------------------------------------------------------

APP_VERSION = "v1.00"
APP_NAME    = f"Photo CD Player {APP_VERSION}"

CONFIG_PATH = os.path.expanduser("~/.config/photocd/config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# IMAGE PACK CONSTANTS (spec Chapter IV)
# ---------------------------------------------------------------------------

# Resolution tiers (spec Section IV.2.1, Fig. IV.1)
RESOLUTIONS = {
    "Base/16": (192,  128),
    "Base/4":  (384,  256),
    "Base":    (768,  512),
    "4Base":   (1536, 1024),
    "16Base":  (3072, 2048),
}

# Image Pack sector layout (spec Section IV.3.2):
# The Image Pack header occupies sector 0 of the file.
# Component data starts immediately after the header sectors.
#
# Subheader File Numbers for image data sectors (spec Section II.3.3):
#   Image Packs use File Numbers 6..255
#   Within an image pack, different tiers use different channel numbers.

# Base image component layout (spec Section IV.3.10/11):
#   Y component:  768 x 512 bytes  = 393,216 bytes
#   Cb component: 384 x 256 bytes  = 98,304 bytes  (half resolution)
#   Cr component: 384 x 256 bytes  = 98,304 bytes  (half resolution)
#   Total Base data: 589,824 bytes = 288 sectors (at 2048 bytes/sector)

BASE_STORED_W  = 768   # stored width (landscape)
BASE_STORED_H  = 512   # stored height
BASE_W         = 768   # display width
BASE_H         = 512   # display height
BASE_Y_SIZE           = BASE_STORED_W * BASE_STORED_H    # 393,216 bytes = 192 sectors
# Chroma: full width, half height (4:2:0 vertical subsampling)
# Layout: row-interleaved [Y, Y, Cb, Cr] × 256 groups
BASE_CHROMA_W         = BASE_STORED_W                    # 768
BASE_CHROMA_H         = BASE_STORED_H // 2               # 256
BASE_CHROMA_SIZE      = BASE_CHROMA_W * BASE_CHROMA_H    # 196,608 bytes

# Confirmed sector layout from disc analysis (IMG0003.PCD):
#   Sector 0:      MRS padding (FF pattern)
#   Sector 1:      IPI header ("PCD_IPI")
#   Sector 2:      Image attributes (zeroed)
#   Sector 3:      MRS padding (FF pattern)
#   Sectors 4-21:  Base/16 data  (192*128 + 96*64*2 = 36864 bytes = 18 sectors)
#   Sectors 22-93: Base/4 data   (384*256 + 192*128*2 = 147456 bytes = 72 sectors)
#   Sector 94:     padding boundary
#   Sector 95:     MRS padding (FF pattern)
#   Sectors 96+:   Base image data (Y plane first, then Cb, then Cr)
BASE_Y_SECTOR_OFFSET  = 96   # confirmed from disc scan


# ---------------------------------------------------------------------------
# PHOTO YCC → RGB CONVERSION (spec Section IV.2.5)
# ---------------------------------------------------------------------------

def build_ycc_to_rgb_lut():
    """
    Pre-compute a lookup table for Photo YCC → sRGB conversion.
    
    Photo YCC color space (spec Section IV.2.5, CCIR 709 primaries):
      Y:  luminance,  0..255
      C1: Cb-like,    0..255 (neutral = 156)
      C2: Cr-like,    0..255 (neutral = 137)

    Matrix (Kodak Photo YCC decode, derived from spec equations):
      r =  Y                    + 1.630 * (C2 - 137) / 255 * 255
      g =  Y - 0.194*(C1-156)  - 0.509 * (C2 - 137)
      b =  Y + 1.990*(C1-156)

    We pre-compute Y, C1, C2 → delta tables then combine at decode time.
    Returns three 256-element arrays: y_lut, c1_lut (for Cb), c2_lut (for Cr).
    """
    import array

    # Gamma LUT: Photo CD stores values in a ~2.2 gamma space.
    # For display we keep sRGB output (monitors expect ~2.2), so we just
    # apply the linear matrix and re-clamp — no additional gamma needed
    # for a simple viewer. A full implementation would linearize first.

    def clamp(v):
        return max(0, min(255, int(v + 0.5)))

    return clamp  # Return clamp helper; we'll do per-pixel conversion inline


def photo_ycc_to_rgb(y_data, c1_data, c2_data, width, height,
                     chroma_w, chroma_h):
    """
    Convert Photo YCC planar components to an RGB PIL Image.

    Photo YCC color encoding (spec Section IV.2.5, CCIR 709 primaries):
      Y:  luminance,        0..255, stored as-is
      C1: Cb (blue-diff),  0..255, neutral = 156 (i.e. 0 chroma = 156)
      C2: Cr (red-diff),   0..255, neutral = 137 (i.e. 0 chroma = 137)

    The correct Kodak Photo YCC → RGB decode matrix is:
      r = Y                        + 1.402  * (C2 - 137)
      g = Y - 0.34414 * (C1 - 156) - 0.71414 * (C2 - 137)
      b = Y + 1.772  * (C1 - 156)

    This is the standard YCbCr→RGB matrix scaled to Photo CD's
    specific neutral offsets (C1=156, C2=137 map to Cb=Cr=0).
    """
    # Pre-build per-value lookup tables for speed (avoid per-pixel multiply)
    # c1_r[v] = contribution of C1 value v to red channel (always 0)
    # c1_g[v] = contribution of C1 value v to green channel
    # c1_b[v] = contribution of C1 value v to blue channel
    # c2_r[v] = contribution of C2 value v to red channel
    # c2_g[v] = contribution of C2 value v to green channel (always 0 from c2)

    c1_g = [round(-0.34414 * (v - 156)) for v in range(256)]
    c1_b = [round( 1.772   * (v - 156)) for v in range(256)]
    c2_r = [round( 1.402   * (v - 137)) for v in range(256)]
    c2_g = [round(-0.71414 * (v - 137)) for v in range(256)]

    def clamp(v):
        return v if 0 <= v <= 255 else (0 if v < 0 else 255)

    rgb = bytearray(width * height * 3)

    for row in range(height):
        chroma_row = row >> 1          # integer divide by 2
        cr_base    = chroma_row * chroma_w
        y_base     = row * width
        out_base   = row * width * 3

        for col in range(width):
            chroma_col = col >> 1
            ci = cr_base + chroma_col

            y  = y_data [y_base + col]
            c1 = c1_data[ci]
            c2 = c2_data[ci]

            r = clamp(y + c2_r[c2])
            g = clamp(y + c1_g[c1] + c2_g[c2])
            b = clamp(y + c1_b[c1])

            oi = out_base + col * 3
            rgb[oi    ] = r
            rgb[oi + 1] = g
            rgb[oi + 2] = b

    return Image.frombytes("RGB", (width, height), bytes(rgb))



# ---------------------------------------------------------------------------
# IMAGE PACK HEADER PARSER (spec Section IV.3.5, Fig. IV.12)
# ---------------------------------------------------------------------------

def parse_image_pack_header(sector_data):
    """
    Parse the Image Pack Information (IPI) header from sector 0 of a .PCD file.
    
    Spec Section IV.3.5.1, Fig. IV.12 — Image Pack Information (IPI):
      The IPI identifies the image format and locates component data.
      
    We extract:
      - Image rotation flag
      - Sector offsets to Base/16, Base/4, Base component data
      
    Returns dict with component sector offsets, or None if header unreadable.
    """
    # IPI magic: first 3 bytes should be "PCD" (0x50 0x43 0x44)
    # Spec Section IV.3.2 — Image Pack starts with Image Pack Attributes (IPA)
    # at the first sector, which contains Image Pack Information (IPI).
    
    if len(sector_data) < 2048:
        return None

    # Check for PCD signature at start of image pack
    # (some implementations put it at a fixed offset)
    magic = sector_data[0:3]
    
    info = {
        "has_pcd_magic": magic == b"PCD",
        "rotation":      0,
        "base_sector":   BASE_FIXED_SECTOR_OFFSET,  # fallback
    }

    if magic == b"PCD":
        # Rotation: byte 12, bits 1-0 (spec Section IV.3.5.1)
        #   0 = 0°, 1 = 90°CCW, 2 = 180°, 3 = 270°CCW
        rotation_code = sector_data[12] & 0x03
        info["rotation"] = [0, 270, 180, 90][rotation_code]

    return info


# ---------------------------------------------------------------------------
# BASE IMAGE READER
# ---------------------------------------------------------------------------

def read_base_image(reader, image_entry, rotation=0):
    """
    Read and decode the Base (768x512) resolution image from a .PCD file.

    Confirmed layout (from autocorrelation analysis of raw byte stream):
      - 288 sectors = 589,824 bytes = 768 rows × 768 bytes/row
      - Pattern: [Y, Y, CbCr] × 256 groups
        * 2 Y rows (768 bytes each) = full luminance
        * 1 CbCr row (768 bytes) with Cb and Cr interleaved:
          [Cb0, Cr0, Cb1, Cr1, ...] — even bytes=Cb, odd bytes=Cr
      - Result: 512 Y rows + 256 Cb rows (384 wide) + 256 Cr rows (384 wide)
    """
    import numpy as np
    import numpy as np

    file_lba = image_entry["lba"]

    W, H   = 768, 512
    CH     = 256   # chroma rows (half height)
    CW     = W // 2  # 384 — Cb and Cr alternate within each chroma row

    # Read all 288 image data sectors as a flat byte stream
    # 288 sectors × 2048 = 589,824 bytes = 768 rows × 768 bytes
    # Pattern confirmed: [Y, Y, CbCr] × 256 groups
    #   Each Y row: 768 Y pixels
    #   Each CbCr row: 384 Cb pixels interleaved with 384 Cr pixels
    #   i.e. [Cb0, Cr0, Cb1, Cr1, ...] or [Cb0, Cb1, ..., Cr0, Cr1, ...]
    base_lba = file_lba + BASE_Y_SECTOR_OFFSET
    print(f"  Reading 288 sectors from LBA {base_lba}...")
    raw = bytearray()
    for s in range(288):
        raw.extend(reader.read_sector(base_lba + s))

    # Parse [Y, Y, CbCr] groups
    y_rows    = []
    cbcr_rows = []
    pos = 0
    for group in range(CH):  # 256 groups
        y_rows.append(raw[pos : pos + W]); pos += W
        y_rows.append(raw[pos : pos + W]); pos += W
        cbcr_rows.append(raw[pos : pos + W]); pos += W

    y_data    = bytes(b for row in y_rows    for b in row)
    cbcr_data = bytes(b for row in cbcr_rows for b in row)

    # Split CbCr row: first 384 bytes = Cb, second 384 bytes = Cr
    # (planar within each chroma row, not interleaved byte-by-byte)
    cbcr = np.frombuffer(cbcr_data, dtype=np.uint8).reshape(CH, W)
    Cb   = cbcr[:, :W//2].astype(np.float32)   # left half  = Cb (384 wide)
    Cr   = cbcr[:, W//2:].astype(np.float32)   # right half = Cr (384 wide)

    print(f"  Y={len(y_data)} bytes  Cb={Cb.shape}  Cr={Cr.shape}")
    print(f"  Cb mean={Cb.mean():.1f} (neutral=156)  Cr mean={Cr.mean():.1f} (neutral=137)")
    print(f"  Converting Photo YCC → RGB...")

    Y = np.frombuffer(y_data, dtype=np.uint8).reshape(H, W).astype(np.float32)

    # Upsample chroma: CH×384 → H×768 (×2 vertically, ×2 horizontally)
    from PIL import Image as PILImage
    Cb_img = PILImage.fromarray(Cb.astype(np.uint8), 'L').resize((W, H), PILImage.BILINEAR)
    Cr_img = PILImage.fromarray(Cr.astype(np.uint8), 'L').resize((W, H), PILImage.BILINEAR)
    Cb_up  = np.array(Cb_img).astype(np.float32)
    Cr_up  = np.array(Cr_img).astype(np.float32)

    # Photo YCC → RGB (spec Section IV.2.5, CCIR 709 primaries)
    # Y is stored in range 16-235 (CCIR 601 video luma range).
    # Cb/Cr neutrals are Photo YCC specific: Cb=156, Cr=137 (not 128).
    Y_scaled = np.clip((Y - 16.0) * (255.0 / 209.0), 0, 255)

    cc1 = Cb_up - 156.0
    cc2 = Cr_up - 137.0
    R_f = np.clip(Y_scaled + 1.402   * cc2,                0, 255)
    G_f = np.clip(Y_scaled - 0.34414 * cc1 - 0.71414 * cc2, 0, 255)
    B_f = np.clip(Y_scaled + 1.772   * cc1,                0, 255)

    GAMMA = 0.70
    R = np.clip(((R_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)
    G = np.clip(((G_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)
    B = np.clip(((B_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)

    img = Image.fromarray(np.stack([R, G, B], axis=2), 'RGB')
    # Apply rotation from Image Descriptor (spec Section III.2.3, bits 1-0 of IPA byte).
    # Values: 0=0°, 1=90° CCW, 2=180°, 3=270° CCW. PIL rotate() is CCW.
    if rotation == 1:
        img = img.rotate(90, expand=True)
    elif rotation == 2:
        img = img.rotate(180, expand=True)
    elif rotation == 3:
        img = img.rotate(270, expand=True)
    return img






# ---------------------------------------------------------------------------
# DISC SETUP — reuse Phase 1 & 2 infrastructure
# ---------------------------------------------------------------------------

def read_info_pcd(reader, photo_cd_entry):
    """
    Parse PHOTO_CD/INFO.PCD and return a dict of disc metadata.

    INFO.PCD layout (spec Section III.2.3):
      Bytes  0- 7: "PHOTO_CD" signature (ISO 646)
      Bytes  8- 9: Spec version (major, minor, binary)
      Bytes 10-21: Disc Serial Number (12 bytes, ISO 646)
      Bytes 22-25: Disc Creation Time (big-endian Unix seconds)
      Bytes 26-29: Disc Modification Time (big-endian Unix seconds)
      Bytes 30-31: Number of Image Packs on disc (big-endian)
      Byte     33: Disc Image Pack Resolution Order
                     bits 7-4 = highest res (0=Base,1=4Base,2=16Base)
                     bits 3-0 = lowest res
      Byte     37: Number of Sessions
      Bytes 38+:   Session Descriptors (68 bytes each):
                     Offset  0- 1: N images in session
                     Offset  8-15: Writer Vendor ID (8 bytes, ISO 646)
                     Offset 16-31: Writer Product ID (16 bytes, ISO 646)
                     Offset 32-35: Firmware Revision Level (ISO 646, "X.YY")
                     Offset 36-43: Firmware Revision Date (ISO 646, "MM/DD/YY")
      After session descriptors: Image Descriptors (6 bytes each):
                     Bytes 0-3: Image Pack Start Pointer (big-endian LBA)
                     Byte  4:   Image Pack Attributes
                                  bits 6-5: 4Base HCT class
                                  bit  4:   IPE present
                                  bits 3-2: resolution order (0=Base,1=4Base,2=16Base)
                                  bits 1-0: required display rotation (0=0°,1=90°,2=180°,3=270° CCW)
                     Byte  5:   Interleaving Ratio

    Returns dict with "image_descriptors" list, or None if INFO.PCD not found / invalid.
    """
    import datetime

    info_entry = find_entry(reader, photo_cd_entry["lba"],
                            photo_cd_entry["size"], "INFO.PCD")
    if not info_entry:
        return None

    try:
        # Read enough sectors to cover header + all image descriptors.
        # Worst case: 38 + 1*68 + 591*6 = 3652 bytes = 2 sectors. Read 8 to be safe.
        n_sectors = min(8, (info_entry["size"] + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE)
        data = bytearray()
        for i in range(n_sectors):
            data.extend(reader.read_sector(info_entry["lba"] + i))
        data = bytes(data)

        if data[0:8] != b"PHOTO_CD":
            return None

        spec_major = data[8]
        spec_minor = data[9]
        serial     = data[10:22].decode("latin-1").strip()
        creation_ts = struct.unpack_from(">I", data, 22)[0]
        n_images   = struct.unpack_from(">H", data, 30)[0]
        res_byte   = data[33]
        n_sessions = data[37]

        creation_date = None
        if creation_ts > 0:
            try:
                creation_date = datetime.datetime.utcfromtimestamp(creation_ts)
            except (OSError, OverflowError):
                pass

        # Session descriptor #1 starts at byte 38
        vendor = product = ""
        if len(data) >= 38 + 68:
            sd = data[38:38 + 68]
            vendor  = sd[8:16].decode("latin-1").replace("\x00", " ").strip()
            product = sd[16:32].decode("latin-1").replace("\x00", " ").strip()

        # Image Descriptors follow all session descriptors (spec Fig. III.4)
        img_desc_offset = 38 + n_sessions * 68
        image_descriptors = []
        for i in range(n_images):
            off = img_desc_offset + i * 6
            if off + 6 > len(data):
                break
            img_lba  = struct.unpack_from(">I", data, off)[0]
            attr     = data[off + 4]
            res_order = (attr >> 2) & 0x03   # bits 3-2
            rotation  = (attr & 0x03)         # bits 1-0: 0=0°,1=90°,2=180°,3=270° CCW
            image_descriptors.append({
                "lba":       img_lba,
                "resolution": res_order,
                "rotation":   rotation,
            })

        return {
            "spec_version":      f"{spec_major}.{spec_minor:02d}",
            "serial":            serial,
            "creation_date":     creation_date,
            "n_images":          n_images,
            "n_sessions":        n_sessions,
            "res_highest":       (res_byte >> 4) & 0x0F,
            "res_lowest":        res_byte & 0x0F,
            "writer_vendor":     vendor,
            "writer_product":    product,
            "image_descriptors": image_descriptors,
        }
    except Exception as e:
        print(f"  INFO.PCD parse error: {e}")
        return None


def _open_disc_kodak_usa(reader, pvd, cue_path, audio_tracks):
    """
    Handle a Kodak Photo CD (USA) disc: High Sierra filesystem, raw uncompressed
    RGB files at Photo CD resolution tiers (_512 = 768×512, _1K = 1536×1024,
    _2K = 3072×2048).

    Returns (reader, images, audio_tracks, audio_start_s, max_resolution, disc_info).
    """
    root_lba, root_size = pvd["root_lba"], pvd["root_size"]
    print(f"  High Sierra filesystem  •  volume: {pvd['volume_id']}")
    print(f"  Root dir: LBA {root_lba}, {root_size} bytes")

    all_entries = list_directory(reader, root_lba, root_size)
    rgb_files   = [e for e in all_entries if e["name"].upper().endswith(".RGB")]
    print(f"  Found {len(rgb_files)} .RGB file(s) in root directory")

    # Group files by base name.
    # Naming convention: IMAGENAME_512.RGB, IMAGENAME_1K.RGB, IMAGENAME_2K.RGB
    # Each suffix maps to a Photo CD resolution tier.
    SUFFIX_MAP = {
        "_2K": (3072, 2048),   # 16Base
        "_1K": (1536, 1024),   # 4Base
        "_512": (768, 512),    # Base
    }
    groups = {}   # base_name → {"2K": entry, "1K": entry, "512": entry}
    for e in rgb_files:
        stem = e["name"].upper()
        if stem.endswith(".RGB"):
            stem = stem[:-4]
        matched = False
        for suffix in SUFFIX_MAP:
            if stem.endswith(suffix):
                base = stem[:-len(suffix)]
                groups.setdefault(base, {})[suffix[1:]] = e   # key: "2K","1K","512"
                matched = True
                break
        if not matched:
            # No recognized suffix — treat as a standalone Base image
            groups.setdefault(stem, {})["512"] = e

    images = []
    for base in sorted(groups.keys()):
        variants = groups[base]
        # Primary entry for the list: prefer 512, then 1K, then 2K
        primary = variants.get("512") or variants.get("1K") or variants.get("2K")
        images.append({
            "name":         base,
            "lba":          primary["lba"],
            "size":         primary["size"],
            "rgb_variants": variants,        # resolution key → dir entry
            "disc_format":  "KodakUSA",
        })
        for key, entry in sorted(variants.items()):
            w, h = SUFFIX_MAP.get(f"_{key}", (768, 512))
            print(f"    {base}_{key}.RGB  {w}×{h}  LBA {entry['lba']}  "
                  f"{entry['size']:,} bytes")

    # Max resolution based on which tiers are present
    any_variants = (list(groups.values()) or [{}])[0]
    if "2K" in any_variants:
        max_resolution = 2
    elif "1K" in any_variants:
        max_resolution = 1
    else:
        max_resolution = 0

    disc_name = os.path.splitext(os.path.basename(cue_path))[0]
    disc_info = {
        "disc_name":    disc_name,
        "disc_format":  "KodakUSA",
        "n_images":     len(images),
        "n_sessions":   1,
    }
    return reader, images, audio_tracks, 0.0, max_resolution, disc_info


def read_raw_rgb_image(reader, image_entry, res="Base"):
    """
    Decode a raw uncompressed RGB image from a Kodak Photo CD (USA) disc.

    The files are plain width×height×3 bytes (R,G,B row-major, no header).
    Resolution 'res' selects which variant to read:
      "Base"  → _512 variant (768×512)
      "4Base" → _1K  variant (1536×1024)
      "16Base"→ _2K  variant (3072×2048)
    """
    import numpy as np

    SUFFIX_FOR_RES = {"Base": "512", "4Base": "1K", "16Base": "2K"}
    DIMS_FOR_SUFFIX = {"512": (768, 512), "1K": (1536, 1024), "2K": (3072, 2048)}

    variants = image_entry.get("rgb_variants", {})
    suffix   = SUFFIX_FOR_RES.get(res, "512")
    entry    = variants.get(suffix)
    if entry is None:
        # Fall back to the lowest available resolution
        for fb in ("512", "1K", "2K"):
            if fb in variants:
                entry  = variants[fb]
                suffix = fb
                break
    if entry is None:
        raise ValueError(f"No RGB variant found for {image_entry['name']!r}")

    w, h = DIMS_FOR_SUFFIX.get(suffix, (768, 512))
    data = reader.read_file(entry["lba"], w * h * 3)
    arr  = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 3))
    from PIL import Image
    return Image.fromarray(arr, "RGB")


def _find_image_packs(reader, root_lba, root_size):
    """
    Scan the root directory and one level of subdirectories for Image Pack
    .PCD files on non-compliant discs that lack a PHOTO_CD/ directory.

    A .PCD file is confirmed as an Image Pack if sector 1 starts with the
    PCD_IPI magic bytes b'PCD_IPI\\x00'.

    Returns a flat list of file entry dicts (name, lba, size), sorted by name.
    """
    PCD_IPI_MAGIC = b"PCD_IPI\x00"

    def is_image_pack(lba):
        try:
            sector = reader.read_sector(lba + 1)
            return sector[:8] == PCD_IPI_MAGIC
        except Exception:
            return False

    def collect_pcd(entries):
        return [e for e in entries
                if e["name"].upper().endswith(".PCD") and is_image_pack(e["lba"])]

    root_entries = list_directory(reader, root_lba, root_size)
    results = collect_pcd(root_entries)

    if not results:
        # Try one level of subdirectories
        for entry in root_entries:
            if entry["is_dir"]:
                try:
                    sub = list_directory(reader, entry["lba"], entry["size"])
                    found = collect_pcd(sub)
                    if found:
                        print(f"  Found {len(found)} Image Pack(s) in {entry['name']}/")
                        results.extend(found)
                except Exception:
                    continue

    return sorted(results, key=lambda x: x["name"])


def _parse_playlist_timings(playlist_data, n_images):
    """
    Extract per-image display timings from PLAYLIST.PCD.

    Returns {image_number: display_time_s} for images with valid timed entries.
    image_number is 1-based, matching the image filename (IMG0001 = 1).

    Bogus entries are filtered out:
      - image_number must be in range 1..n_images
      - display_time must be between 0.5s and 3600s
    For duplicates, keeps the longest valid timing.
    """
    try:
        from photocd_playlist_parse import find_all_play_sequences
        sequences = find_all_play_sequences(playlist_data)
    except Exception:
        return {}

    timings = {}
    for seq in sequences:
        for img in seq["images"]:
            num = img["number"]
            t   = img["display_time_s"]
            if 1 <= num <= n_images and t and 0.5 <= t <= 3600:
                if num not in timings or t > timings[num]:
                    timings[num] = t
    return timings


class _StandaloneFileReader:
    """Minimal reader that serves a single flat file as 2048-byte logical sectors."""
    def __init__(self, data):
        self._data = data

    def read_sector(self, lba):
        """Return 2048 bytes of user data at the given logical sector."""
        off = lba * 2048
        chunk = self._data[off:off+2048]
        return chunk + b'\x00' * max(0, 2048 - len(chunk))

    def read_file(self, lba, size):
        """Read `size` raw bytes starting at logical sector `lba`."""
        off = lba * 2048
        return self._data[off:off + size]

    def close(self):
        self._data = b''


def _open_standalone_file(path):
    """Open a single .PCD (Image Pack) or .RGB (raw Kodak USA) file directly."""
    ext  = os.path.splitext(path)[1].upper()
    name = os.path.basename(path)
    data = open(path, "rb").read()
    reader = _StandaloneFileReader(data)

    if ext == ".PCD":
        image_entry = {"name": name, "lba": 0, "size": len(data)}
        images = [image_entry]
        disc_info = {"disc_name": os.path.splitext(name)[0], "n_images": 1}

        max_resolution = 0
        try:
            from photocd_hires import _read_ipa_byte, _get_resolution_order
            ipa = _read_ipa_byte(reader, 0)
            max_resolution = _get_resolution_order(ipa)
        except Exception:
            pass

        return reader, images, [], 0.0, max_resolution, disc_info

    elif ext == ".RGB":
        stem = os.path.splitext(name)[0].upper()
        SUFFIX_TO_RES = {"_2K": 2, "_1K": 1, "_512": 0}
        max_resolution = 0
        res_key = "512"
        for sfx, res in SUFFIX_TO_RES.items():
            if stem.endswith(sfx):
                max_resolution = res
                res_key = sfx[1:]
                break

        base = stem
        for sfx in SUFFIX_TO_RES:
            if base.endswith(sfx):
                base = base[:-len(sfx)]
                break

        image_entry = {
            "name":         base,
            "lba":          0,
            "size":         len(data),
            "rgb_variants": {res_key: {"lba": 0, "size": len(data)}},
            "disc_format":  "KodakUSA",
        }
        images = [image_entry]
        disc_info = {
            "disc_name":   os.path.splitext(name)[0],
            "disc_format": "KodakUSA",
            "n_images":    1,
        }
        return reader, images, [], 0.0, max_resolution, disc_info

    else:
        raise ValueError(f"Unsupported file type: {ext}")


def open_disc(cue_path):
    """Open the disc and return (reader, images_list, audio_tracks, audio_start_s, max_resolution, disc_info)."""
    tracks, data_track, audio_tracks = map_disc(cue_path)
    if not data_track:
        print("ERROR: No data track.")
        return None, [], [], 0.0, 0, {}

    if not validate_bin_files(tracks):
        return None, [], [], 0.0, 0, {}

    # Build a reader that spans all data tracks from the PVD track onward.
    # On most discs there is only one data track, but some (e.g. CD-i hybrid
    # discs like Alice in Cyberland) have a short stub Track 01 + a main
    # Track 02, with image LBAs that exceed Track 01's bin size.
    data_tracks_in_volume = [t for t in tracks
                              if "MODE" in t["type"]
                              and t["number"] >= data_track["number"]]
    reader = MultiTrackReader(data_tracks_in_volume)

    try:
        pvd = read_pvd(reader)
    except Exception as e:
        print(f"ERROR reading PVD: {e}")
        reader.close()
        return None, [], [], 0.0, 0, {}

    # Kodak Photo CD (USA): early pre-spec discs with High Sierra filesystem
    # and raw uncompressed RGB files instead of standard Image Packs.
    if pvd.get("disc_format") == "HighSierra":
        return _open_disc_kodak_usa(reader, pvd, cue_path, audio_tracks)

    root_lba, root_size = pvd["root_lba"], pvd["root_size"]
    photo_cd = find_entry(reader, root_lba, root_size, "PHOTO_CD")

    if photo_cd:
        images_dir = find_entry(reader, photo_cd["lba"], photo_cd["size"], "IMAGES")
        if not images_dir:
            print("ERROR: PHOTO_CD/IMAGES/ not found.")
            reader.close()
            return None, [], [], 0.0, 0, {}
        all_entries = list_directory(reader, images_dir["lba"], images_dir["size"])
    else:
        # Non-compliant disc: no PHOTO_CD/ directory.
        # Scan root and one level of subdirectories for Image Pack .PCD files
        # (identified by PCD_IPI\x00 magic at sector 1, byte 0).
        print("  NOTE: No PHOTO_CD/ directory — scanning for Image Pack .PCD files.")
        all_entries = _find_image_packs(reader, root_lba, root_size)
        if not all_entries:
            print("ERROR: No Image Pack .PCD files found.")
            reader.close()
            return None, [], [], 0.0, 0, {}

    images = sorted(
        [e for e in all_entries if e["name"].upper().endswith(".PCD")],
        key=lambda x: x["name"]
    )

    # INFO.PCD — disc metadata, image descriptors (resolution + rotation per image)
    disc_info = read_info_pcd(reader, photo_cd) if photo_cd else None

    # Derive max_resolution from the actual IPA byte inside the first image pack.
    # Both the disc-level res_highest field and INFO.PCD Image Descriptors are
    # unreliable on some mastering systems (e.g. IMS CDD521/02 sets 16Base in
    # metadata but encodes only Base data). The IPA byte in the image pack
    # sector is the only ground truth.
    max_resolution = 0
    if images:
        try:
            from photocd_hires import _read_ipa_byte, _get_resolution_order
            ipa = _read_ipa_byte(reader, images[0]["lba"])
            max_resolution = _get_resolution_order(ipa)
        except Exception:
            # Fall back to INFO.PCD Image Descriptors if IPA read fails
            if disc_info and disc_info.get("image_descriptors"):
                max_resolution = max(d["resolution"] for d in disc_info["image_descriptors"])
            elif disc_info:
                max_resolution = disc_info.get("res_highest", 0)

    res_names = {0: "Base (768×512)", 1: "4Base (1536×1024)"}
    res_label = res_names.get(max_resolution, "16Base (3072×2048)") if max_resolution < 2 else "16Base (3072×2048)"
    print(f"  Disc image resolution: {res_label}")

    if disc_info:
        serial = disc_info["serial"] or "(no serial)"
        date   = disc_info["creation_date"]
        date_s = date.strftime("%Y-%m-%d") if date else "unknown date"
        print(f"  Disc: {serial}  •  {date_s}"
              f"  •  spec {disc_info['spec_version']}"
              f"  •  {disc_info['n_images']} images"
              f"  •  {disc_info['n_sessions']} session(s)")
        if disc_info["writer_vendor"] or disc_info["writer_product"]:
            print(f"  Writer: {disc_info['writer_vendor']} {disc_info['writer_product']}")

    # Attach disc name from .cue filename to disc_info
    disc_name = os.path.splitext(os.path.basename(cue_path))[0]
    if disc_info:
        disc_info["disc_name"] = disc_name
    else:
        disc_info = {"disc_name": disc_name}

    # PLAYLIST.PCD — per-image display timings for timed slideshow
    image_timings = {}
    playlist_entry = find_entry(reader, photo_cd["lba"], photo_cd["size"], "PLAYLIST.PCD") if photo_cd else None
    if playlist_entry:
        try:
            playlist_data = reader.read_file(playlist_entry["lba"], playlist_entry["size"])
            image_timings = _parse_playlist_timings(playlist_data, len(images))
            if image_timings:
                print(f"  Playlist: {len(image_timings)} timed image(s)")
        except Exception as e:
            print(f"  WARNING: PLAYLIST.PCD parse error: {e}")
    disc_info["image_timings"] = image_timings

    audio_start_s = 0.0

    return reader, images, audio_tracks, audio_start_s, max_resolution, disc_info


def _parse_playlist_audio_start(data):
    """Extract the CD-DA clip start time from PLAYLIST.PCD.
    Returns start time in seconds, or 0.0 if not found / manual.
    """
    import struct

    def bcd(b):
        return (b >> 4) * 10 + (b & 0x0f)

    # Scan for first Play Sequence header (0x20-0x2F) with valid n_images
    for pos in range(len(data) - 3):
        if (data[pos] >> 4) == 0x2:
            n_images = struct.unpack_from('>H', data, pos + 1)[0]
            if 0 < n_images <= 200:
                # CD-DA entry follows: header(1) + n_images_field(2) +
                # next(4) + prev(4) + reserved(22) + n_images*16 bytes
                cdda_pos = pos + 1 + 2 + 4 + 4 + 22 + n_images * 16
                if cdda_pos + 6 <= len(data):
                    m, s, f = data[cdda_pos], data[cdda_pos+1], data[cdda_pos+2]
                    # Sanity check: not all-zero and not all-FF
                    if (m, s, f) != (0, 0, 0) and (m, s, f) != (0xFF, 0xFF, 0xFF):
                        start_s = bcd(m) * 60 + bcd(s) + bcd(f) / 75.0
                        if 0 < start_s < 7200:  # must be a plausible time
                            return start_s
                break  # only use first play sequence
    return 0.0


# ---------------------------------------------------------------------------
# AUDIO PLAYER
# ---------------------------------------------------------------------------

class CDDAStream:
    """
    File-like wrapper that presents a raw CD-DA .bin as a WAV stream.
    Prepends a 44-byte WAV header; reads PCM directly from the .bin on demand.
    No temp file written — pygame.mixer.music.load() accepts file-like objects.
    """
    _SAMPLE_RATE  = 44100
    _CHANNELS     = 2
    _SAMPLE_WIDTH = 2   # bytes (16-bit)

    def __init__(self, bin_path, start_byte, pcm_bytes):
        self._f          = open(bin_path, 'rb')
        self._start      = start_byte
        self._pcm_bytes  = pcm_bytes
        self._header     = self._make_header(pcm_bytes)
        self._hdr_size   = len(self._header)
        self._total      = self._hdr_size + pcm_bytes
        self._pos        = 0

    @classmethod
    def _make_header(cls, pcm_bytes):
        sr, ch, sw = cls._SAMPLE_RATE, cls._CHANNELS, cls._SAMPLE_WIDTH
        return struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + pcm_bytes, b'WAVE',
            b'fmt ', 16, 1, ch, sr, sr * ch * sw, ch * sw, sw * 8,
            b'data', pcm_bytes)

    def read(self, n=-1):
        if n < 0:
            n = self._total - self._pos
        if n <= 0 or self._pos >= self._total:
            return b''
        result = bytearray()
        rem = n
        if self._pos < self._hdr_size:
            chunk = self._header[self._pos:self._pos + rem]
            result.extend(chunk)
            self._pos += len(chunk)
            rem -= len(chunk)
        if rem > 0 and self._pos < self._total:
            self._f.seek(self._start + (self._pos - self._hdr_size))
            chunk = self._f.read(rem)
            result.extend(chunk)
            self._pos += len(chunk)
        return bytes(result)

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._total + offset
        self._pos = max(0, min(self._pos, self._total))
        return self._pos

    def tell(self):
        return self._pos

    def close(self):
        self._f.close()


class AudioPlayer:
    """
    Plays Redbook CD-DA audio from a .bin file using pygame.
    Streams directly from the .bin without writing a temp file.

    Redbook CD-DA: 2352 bytes/sector, 16-bit signed stereo PCM,
    44100 Hz, little-endian, interleaved L/R samples.
    """

    def __init__(self, audio_tracks, start_s=0.0):
        self.audio_tracks = audio_tracks
        self.start_s      = start_s   # disc-specified audio start position
        self.stop_flag    = False
        self.initialized  = False
        self._stream      = None

        if not audio_tracks:
            print("  No audio tracks found.")
            return

        try:
            import pygame
            pygame.mixer.pre_init(44100, -16, 2, 4096)
            pygame.mixer.init()
            self.initialized = True
            print(f"  pygame.mixer initialized (44100 Hz, 16-bit stereo)")
        except Exception as e:
            print(f"  pygame not available: {e}  →  pip install pygame")

    def play(self):
        """Stream audio directly from .bin via CDDAStream (no temp file)."""
        if not self.initialized or not self.audio_tracks:
            return

        try:
            import pygame

            track    = self.audio_tracks[0]
            bin_path = track["bin_file"]
            duration = track["duration"]  # sector count

            SECTOR      = 2352
            start_byte  = int(self.start_s * 75) * SECTOR
            total_bytes = duration * SECTOR
            if start_byte >= total_bytes:
                start_byte = 0

            print(f"  ♪ Streaming audio from {os.path.basename(bin_path)}")

            self._stream = CDDAStream(bin_path, start_byte, total_bytes - start_byte)
            pygame.mixer.music.load(self._stream)
            pygame.mixer.music.play()

        except Exception as e:
            print(f"  Audio play error: {e}")
            import traceback; traceback.print_exc()

    def stop(self):
        self.stop_flag = True
        if self.initialized:
            try:
                import pygame
                pygame.mixer.music.stop()
                pygame.mixer.quit()
            except Exception:
                pass
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def is_playing(self):
        if not self.initialized:
            return False
        try:
            import pygame
            return pygame.mixer.music.get_busy()
        except Exception:
            return False


# ---------------------------------------------------------------------------
# TKINTER VIEWER
# ---------------------------------------------------------------------------

class PhotoCDViewer:
    """
    Minimal Photo CD-style viewer window.
    Shows one image at a time with Prev/Next navigation.
    Styled to evoke the original Kodak Photo CD player interface.
    """

    def __init__(self, root, reader, images, audio_player=None,
                 image_timings=None, hires_decode_fn=None, disc_info=None,
                 start_index=0, max_resolution=0):
        self.root            = root
        self.reader          = reader
        self.images          = images
        self.index           = start_index
        self.img_cache       = {}
        self.audio_player    = audio_player
        self.image_timings   = image_timings or {}
        self.slideshow_on    = False
        self.slideshow_id    = None
        self.hires_decode_fn = hires_decode_fn  # None = Base only
        self.disc_info       = disc_info
        self.max_resolution  = max_resolution
        self.res_var         = tk.StringVar(value=self._default_res(max_resolution))
        self.config          = load_config()
        self._current_pil    = None
        self._current_name   = ""
        self._current_res    = ""

        self._disc_title = self._make_disc_title(disc_info)

        title = f"Photo CD Player — {self._disc_title}" if self._disc_title else APP_NAME
        self.root.title(title)
        self.root.configure(bg="#1a1a1a")
        self.root.resizable(True, True)
        self.root.geometry("1280x860")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._set_disc_loaded_ui(bool(self.images))
        if self.images:
            self._load_image(self.index)

        # Start audio after UI is ready
        if self.audio_player:
            self.root.after(500, self.audio_player.play)

    def _on_close(self):
        if self.audio_player:
            self.audio_player.stop()
        self.root.destroy()

    def _build_ui(self):
        C = {
            "bg":      "#1a1a1a",
            "sidebar": "#141414",
            "toolbar": "#212121",
            "canvas":  "#000000",
            "fg":      "#B7B7B7",
            "fg_dim":  "#B7B7B7",
            "fg_acc":  "#f4c430",
            "btn":     "#B7B7B7",
            "btn_h":   "#D0D0D0",
            "btn_fg":  "#1E1E1E",
            "sep":     "#383838",
        }
        self._C = C

        # Button factory — Label-based so macOS respects bg color
        def ibtn(parent, text, cmd, width=2, font_size=11):
            lbl = tk.Label(parent, text=text,
                           bg=C["btn"], fg=C["btn_fg"],
                           font=("Helvetica", font_size), padx=8, pady=2,
                           cursor="hand2", width=width)
            lbl._enabled = True
            orig_config = lbl.config

            def on_click(e):
                if lbl._enabled:
                    cmd()

            def on_enter(e):
                if lbl._enabled:
                    orig_config(bg=C["btn_h"])

            def on_leave(e):
                orig_config(bg=C["btn"])

            def patched_config(**kwargs):
                state = kwargs.pop("state", None)
                if state == tk.DISABLED:
                    lbl._enabled = False
                    orig_config(fg=C["fg_dim"], cursor="arrow")
                elif state == tk.NORMAL:
                    lbl._enabled = True
                    orig_config(fg=C["btn_fg"], cursor="hand2")
                if kwargs:
                    orig_config(**kwargs)

            lbl.bind("<Button-1>", on_click)
            lbl.bind("<Enter>", on_enter)
            lbl.bind("<Leave>", on_leave)
            lbl.config = patched_config
            return lbl

        # ── Root layout: sidebar | main ──────────────────────────────────────
        self._fullscreen = False
        outer = tk.Frame(self.root, bg=C["bg"])
        outer.pack(fill=tk.BOTH, expand=True)

        # Left thumbnail sidebar — hidden by default
        self._filmstrip_visible = False
        self._sidebar = tk.Frame(outer, bg=C["sidebar"], width=112)
        # Not packed initially — shown via Film Strip button
        self._sidebar.pack_propagate(False)

        # Thin custom scrollbar (6px) — avoids white macOS tk.Scrollbar
        _SB_W = 6
        self._sb_canvas = tk.Canvas(self._sidebar, bg=C["sidebar"],
                                    highlightthickness=0, width=_SB_W)
        self._sb_canvas.pack(side=tk.RIGHT, fill=tk.Y)

        self._thumb_canvas = tk.Canvas(self._sidebar, bg=C["sidebar"],
                                       highlightthickness=0)
        self._thumb_canvas.pack(fill=tk.BOTH, expand=True)

        def _sb_draw(*_):
            self._sb_canvas.delete("all")
            try:
                top, bot = self._thumb_canvas.yview()
            except Exception:
                return
            if bot - top >= 1.0:
                return
            h = self._sb_canvas.winfo_height()
            y0 = int(top * h)
            y1 = int(bot * h)
            self._sb_canvas.create_rectangle(1, y0, _SB_W - 1, y1,
                                             fill=C["btn"], outline="")

        def _sb_scroll(frac):
            self._thumb_canvas.yview_moveto(frac)
            _sb_draw()

        def _sb_click(e):
            h = self._sb_canvas.winfo_height()
            _sb_scroll(e.y / h)

        self._sb_canvas.bind("<Button-1>", _sb_click)
        self._sb_canvas.bind("<B1-Motion>", _sb_click)

        self._thumb_inner = tk.Frame(self._thumb_canvas, bg=C["sidebar"])
        self._thumb_window = self._thumb_canvas.create_window(
            (0, 0), window=self._thumb_inner, anchor="nw")
        self._thumb_inner.bind("<Configure>",
            lambda e: [self._thumb_canvas.configure(
                scrollregion=self._thumb_canvas.bbox("all")), _sb_draw()])
        self._thumb_canvas.bind("<MouseWheel>",
            lambda e: [self._thumb_canvas.yview_scroll(-1 * (e.delta // 120), "units"),
                       _sb_draw()])
        self._thumb_canvas.configure(yscrollcommand=lambda *a: _sb_draw())
        self._thumb_labels = []

        # Right: image area + bottom bar
        right = tk.Frame(outer, bg=C["bg"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._right_frame = right

        # Image canvas
        self.canvas = tk.Canvas(right, bg=C["canvas"], highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # Library browser — shown when no disc is loaded
        self._lib_frame = tk.Frame(self.canvas, bg=C["bg"])
        self._lib_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        tk.Label(self._lib_frame, text="Photo CD Library",
                 fg=C["fg_dim"], bg=C["bg"],
                 font=("Helvetica", 16, "bold")).pack(pady=(30, 10))

        lib_list_frame = tk.Frame(self._lib_frame, bg=C["bg"])
        lib_list_frame.pack(fill=tk.BOTH, expand=True, padx=40, pady=(0, 20))

        self._lib_msg = tk.Label(lib_list_frame,
                                 text='Go to "Settings" → "Set Library Location..." and navigate\nwhere your Photo CD files are kept.',
                                 fg=C["fg_dim"], bg=C["bg"],
                                 font=("Helvetica", 13), justify=tk.CENTER)
        # shown only when no library is configured; hidden once titles load

        self._lib_listbox = tk.Listbox(lib_list_frame,
                                       bg=C["sidebar"], fg=C["fg_dim"],
                                       selectbackground=C["btn"], selectforeground=C["btn_fg"],
                                       font=("Helvetica", 13), relief=tk.FLAT,
                                       highlightthickness=0, borderwidth=0,
                                       activestyle="none", cursor="hand2")
        self._lib_listbox.bind("<Double-Button-1>", self._on_lib_select)
        self._lib_listbox.bind("<Return>", self._on_lib_select)

        self._lib_cue_paths = []
        self.root.after(100, self._populate_library_list)

        # ── Bottom toolbar ────────────────────────────────────────────────────
        bar = tk.Frame(right, bg=C["toolbar"])
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(right, bg=C["sep"], height=1).pack(fill=tk.X, side=tk.BOTTOM)

        # Disc title + status — full width info strip above buttons
        info_row = tk.Frame(bar, bg=C["toolbar"])
        info_row.pack(fill=tk.X, pady=(4, 0))

        self.lbl_disc_title = tk.Label(info_row, text=self._disc_title or "",
                                       fg=C["fg_dim"], bg=C["toolbar"],
                                       font=("Helvetica", 11, "bold"))
        self.lbl_disc_title.pack(side=tk.LEFT, expand=True, anchor="e")

        self.lbl_status = tk.Label(info_row, text="Double click a Library title or open a file to load a disc.",
                                   fg=C["fg_dim"], bg=C["toolbar"],
                                   font=("Helvetica", 11))
        self.lbl_status.pack(side=tk.LEFT, expand=True, anchor="w", padx=(6, 0))

        # Button row — centered horizontally, vertically padded
        btn_row = tk.Frame(bar, bg=C["toolbar"])
        btn_row.pack(expand=True, pady=6)

        # Disc-specific controls — hidden until a disc is loaded
        self._disc_frame = tk.Frame(btn_row, bg=C["toolbar"])
        # (not packed here; _set_disc_loaded_ui controls visibility)

        # Nav
        self.btn_prev = ibtn(self._disc_frame, "◀", self.prev_image, width=3)
        self.btn_prev.pack(side=tk.LEFT, padx=1)
        self.btn_next = ibtn(self._disc_frame, "▶", self.next_image, width=3)
        self.btn_next.pack(side=tk.LEFT, padx=1)

        # Image counter
        self.lbl_count = tk.Label(self._disc_frame, text="", fg=C["fg_dim"],
                                  bg=C["toolbar"], font=("Helvetica", 11))
        self.lbl_count.pack(side=tk.LEFT, padx=(6, 0))

        tk.Frame(self._disc_frame, bg=C["sep"], width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6)

        # Custom volume slider
        vol_grp = tk.Frame(btn_row, bg=C["toolbar"])
        vol_grp.pack(side=tk.LEFT)

        _assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        if _CAIROSVG_OK:
            def _svg_img(name, h=18):
                path = os.path.join(_assets_dir, name)
                with open(path, "rb") as _f:
                    svg_data = _f.read()
                png = _cairosvg.svg2png(bytestring=svg_data, output_height=h)
                return ImageTk.PhotoImage(Image.open(_io.BytesIO(png)).convert("RGBA"))
            self._img_vol_lo = _svg_img("sound_lo.svg")
            self._img_vol_hi = _svg_img("sound_hi.svg")
            tk.Label(vol_grp, image=self._img_vol_lo,
                     bg=C["toolbar"]).pack(side=tk.LEFT, padx=(4, 2))
        else:
            tk.Label(vol_grp, text="🔈", fg=C["fg_dim"], bg=C["toolbar"],
                     font=("Helvetica", 11)).pack(side=tk.LEFT, padx=(4, 2))

        self._vol_value = 100  # 0–100
        _VOL_W, _VOL_H = 90, 18
        _TRACK_H = 4
        _HANDLE_R = 6

        vol_canvas = tk.Canvas(vol_grp, width=_VOL_W, height=_VOL_H,
                               bg=C["toolbar"], highlightthickness=0, cursor="hand2")
        vol_canvas.pack(side=tk.LEFT, padx=2)

        def _vol_draw(val):
            vol_canvas.delete("all")
            cx = int(val / 100 * _VOL_W)
            ty = _VOL_H // 2
            # trough background
            vol_canvas.create_rectangle(0, ty - _TRACK_H//2, _VOL_W, ty + _TRACK_H//2,
                                        fill=C["sep"], outline="", tags="track")
            # filled portion
            if cx > 0:
                vol_canvas.create_rectangle(0, ty - _TRACK_H//2, cx, ty + _TRACK_H//2,
                                            fill=C["btn_h"], outline="", tags="fill")
            # handle
            vol_canvas.create_oval(cx - _HANDLE_R, ty - _HANDLE_R,
                                   cx + _HANDLE_R, ty + _HANDLE_R,
                                   fill=C["btn"], outline=C["btn_h"], width=1, tags="handle")

        def _vol_set(x):
            val = max(0, min(100, int(x / _VOL_W * 100)))
            self._vol_value = val
            _vol_draw(val)
            self._on_volume(val)

        vol_canvas.bind("<Button-1>", lambda e: _vol_set(e.x))
        vol_canvas.bind("<B1-Motion>", lambda e: _vol_set(e.x))

        _vol_draw(self._vol_value)
        self._vol_canvas_draw = _vol_draw

        if _CAIROSVG_OK:
            tk.Label(vol_grp, image=self._img_vol_hi,
                     bg=C["toolbar"]).pack(side=tk.LEFT, padx=(2, 4))
        else:
            tk.Label(vol_grp, text="🔊", fg=C["fg_dim"], bg=C["toolbar"],
                     font=("Helvetica", 11)).pack(side=tk.LEFT, padx=(2, 4))

        tk.Frame(btn_row, bg=C["sep"], width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        # Resolution selector — inside disc_frame
        self.res_frame = tk.Frame(self._disc_frame, bg=C["toolbar"])
        tk.Label(self.res_frame, text="Resolution:", bg=C["toolbar"], fg=C["fg_dim"],
                 font=("Helvetica", 11)).pack(side=tk.LEFT, padx=(0, 2))
        choices = self._res_choices(self.max_resolution)
        self.res_menu = tk.OptionMenu(self.res_frame, self.res_var, *choices,
                                      command=self._on_res_change)
        self.res_menu.config(bg=C["btn"], fg=C["btn_fg"],
                             activebackground=C["btn_h"], activeforeground=C["btn_fg"],
                             highlightthickness=0, font=("Helvetica", 11),
                             relief=tk.FLAT, cursor="hand2", width=6,
                             pady=3)
        self.res_menu["menu"].config(bg=C["btn"], fg=C["btn_fg"])
        self.res_menu.pack(side=tk.LEFT)
        self.res_frame.pack(side=tk.LEFT, padx=2)

        # _disc_frame (nav + counter + sep + resolution) — visibility managed by _set_disc_loaded_ui
        # (not packed here; will be packed before btn_library in _set_disc_loaded_ui)

        # Button order: Library | Open .cue | Fullscreen | Save .png | Settings
        # Library and Save .png are disc-specific; the rest are always visible.
        self.btn_library = ibtn(btn_row, "Library", self._show_library, width=7)

        self._open_cue_btn = ibtn(btn_row, "Open File", self.open_cue, width=8)
        self._open_cue_btn.pack(side=tk.LEFT, padx=2)

        self.btn_fullscreen = ibtn(btn_row, "Fullscreen", self.toggle_fullscreen, width=9)
        self.btn_fullscreen.pack(side=tk.LEFT, padx=2)

        self.btn_save = ibtn(btn_row, "Save .png", self.save_image, width=8)

        self._filmstrip_var = tk.BooleanVar(value=False)
        self.btn_settings = ibtn(btn_row, "Settings", self._open_settings_menu, width=7)
        self.btn_settings.pack(side=tk.LEFT, padx=2)

        # Slideshow button
        self.btn_slide = None
        if self.image_timings:
            self.btn_slide = ibtn(btn_row, "▶", self.toggle_slideshow, width=2)
            self.btn_slide.pack(side=tk.LEFT, padx=1)

        # Keyboard bindings
        self.root.bind("<Left>",      lambda e: self.prev_image())
        self.root.bind("<Right>",     lambda e: self.next_image())
        self.root.bind("<Escape>",    lambda e: self._exit_fullscreen_or_quit())
        self.root.bind("s",           lambda e: self.save_image())
        self.root.bind("S",           lambda e: self.save_image())
        self.root.bind("<Control-o>", lambda e: self.open_cue())
        self.root.bind("f",           lambda e: self.toggle_fullscreen())
        self.root.bind("F",           lambda e: self.toggle_fullscreen())
        self.root.bind("<F11>",       lambda e: self.toggle_fullscreen())
        if self.image_timings:
            self.root.bind("<space>", lambda e: self.toggle_slideshow())

    def _open_settings_menu(self):
        C = self._C
        menu = tk.Menu(self.root, tearoff=0,
                       bg=C["btn"], fg=C["btn_fg"],
                       activebackground=C["btn_h"], activeforeground=C["btn_fg"],
                       font=("Helvetica", 11))
        menu.add_checkbutton(label="Film Strip", variable=self._filmstrip_var,
                             command=self._toggle_filmstrip)
        menu.add_command(label="Set Library Location...", command=self.set_library_dir)
        menu.add_command(label="Set Save .png Location...", command=self.set_save_dir)
        x = self.btn_settings.winfo_rootx()
        y = self.btn_settings.winfo_rooty()
        menu.post(x, y)

    def _on_volume(self, val):
        try:
            import pygame
            pygame.mixer.music.set_volume(int(val) / 100.0)
        except Exception:
            pass


    def _toggle_filmstrip(self):
        self._filmstrip_visible = self._filmstrip_var.get()
        if self._filmstrip_visible:
            self._sidebar.pack(side=tk.LEFT, fill=tk.Y,
                               before=self._right_frame)
            # Load thumbnails if not yet loaded
            if not self._thumb_labels and self.images:
                self._load_thumbnails_async()
        else:
            self._sidebar.pack_forget()
        self.root.after(50, self._redraw)

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)
        if self._fullscreen:
            if self._filmstrip_visible:
                self._sidebar.pack_forget()
        else:
            if self._filmstrip_visible:
                self._sidebar.pack(side=tk.LEFT, fill=tk.Y,
                                   before=self._sidebar.master.winfo_children()[0])
        self.btn_fullscreen.config(text="Exit Full" if self._fullscreen else "Fullscreen")
        self.root.after(50, self._redraw)

    def _exit_fullscreen_or_quit(self):
        if self._fullscreen:
            self.toggle_fullscreen()
        else:
            self.root.quit()

    def _load_thumbnails_async(self):
        """Load OVERVIEW.PCD thumbnails in a background thread and populate sidebar."""
        import threading
        threading.Thread(target=self._load_thumbnails_thread, daemon=True).start()

    def _load_thumbnails_thread(self):
        try:
            from photocd_overview import decode_thumbnail, THUMB_SECTORS, THUMB_W, THUMB_H
            import struct

            photo_cd = None
            # Find PHOTO_CD directory via the existing reader
            from photocd_fs_reader import read_pvd, find_entry
            pvd = read_pvd(self.reader)
            photo_cd = find_entry(self.reader, pvd["root_lba"], pvd["root_size"], "PHOTO_CD")
            if not photo_cd:
                return

            overview = find_entry(self.reader, photo_cd["lba"], photo_cd["size"], "OVERVIEW.PCD")
            if not overview:
                return

            opa = self.reader.read_sector(overview["lba"])
            n_images = struct.unpack_from(">H", opa, 9)[0]
            if n_images == 0 or n_images > 9999:
                n_images = len(self.images)

            DISPLAY_W = 96
            DISPLAY_H = 64

            for i in range(min(n_images, len(self.images))):
                if self.reader is None:
                    break
                thumb_lba = overview["lba"] + 5 + i * THUMB_SECTORS
                data = bytearray()
                for s in range(THUMB_SECTORS):
                    data.extend(self.reader.read_sector(thumb_lba + s))
                thumb = decode_thumbnail(bytes(data))
                thumb_small = thumb.resize((DISPLAY_W, DISPLAY_H), Image.LANCZOS)
                tk_img = ImageTk.PhotoImage(thumb_small)
                # Schedule UI update on main thread
                self.root.after(0, self._add_thumb_label, i, tk_img)
        except Exception as e:
            print(f"  Thumbnail sidebar error: {e}")

    def _add_thumb_label(self, index, tk_img):
        C = self._C
        frame = tk.Frame(self._thumb_inner, bg=C["sidebar"], cursor="hand2")
        frame.pack(fill=tk.X, padx=4, pady=2)

        lbl = tk.Label(frame, image=tk_img, bg=C["sidebar"],
                       borderwidth=2, relief=tk.FLAT, cursor="hand2")
        lbl.image = tk_img  # keep reference
        lbl.pack(padx=0, pady=0)

        num_lbl = tk.Label(frame, text=str(index + 1), fg=C["fg_dim"],
                           bg=C["sidebar"], font=("Helvetica", 8))
        num_lbl.pack()

        def on_click(i=index):
            self.index = i
            self._load_image(i)

        lbl.bind("<Button-1>", lambda e, i=index: on_click(i))
        frame.bind("<Button-1>", lambda e, i=index: on_click(i))
        num_lbl.bind("<Button-1>", lambda e, i=index: on_click(i))

        # Extend list if needed
        while len(self._thumb_labels) <= index:
            self._thumb_labels.append(None)
        self._thumb_labels[index] = (frame, lbl)
        self._highlight_thumb(self.index)

    def _highlight_thumb(self, index):
        C = self._C
        for i, item in enumerate(self._thumb_labels):
            if item is None:
                continue
            frame, lbl = item
            if i == index:
                lbl.config(bg="#f4c430", borderwidth=2, relief=tk.SOLID)
                frame.config(bg=C["sidebar"])
                # Scroll thumbnail into view
                self._thumb_canvas.update_idletasks()
                bbox = self._thumb_canvas.bbox("all")
                if bbox:
                    total_h = bbox[3]
                    n = len(self._thumb_labels)
                    if n > 0:
                        frac = index / n
                        self._thumb_canvas.yview_moveto(max(0.0, frac - 0.1))
            else:
                lbl.config(bg=C["sidebar"], borderwidth=2, relief=tk.FLAT)

    @staticmethod
    def _make_disc_title(disc_info):
        if not disc_info:
            return None
        return disc_info.get("disc_name") or None

    @staticmethod
    def _res_choices(max_resolution):
        choices = ["Base"]
        if max_resolution >= 1:
            choices.append("4Base")
        if max_resolution >= 2:
            choices.append("16Base")
        return choices

    @staticmethod
    def _default_res(max_resolution):
        if max_resolution >= 2:
            return "16Base"
        if max_resolution == 1:
            return "4Base"
        return "Base"

    def _on_res_change(self, _val=None):
        if self.images:
            self._load_image(self.index)

    def _load_image(self, index):
        entry = self.images[index]
        name  = entry["name"]
        lba   = entry["lba"]

        self.lbl_status.config(text=f"Loading {name}...")
        self.lbl_count.config(text=f"{index + 1} / {len(self.images)}")
        self.root.update()

        res = self.res_var.get()
        cache_key = (lba, res)

        if cache_key not in self.img_cache:
            try:
                # Kodak Photo CD (USA): raw uncompressed RGB files, no Image Packs
                if entry.get("disc_format") == "KodakUSA":
                    pil_img  = read_raw_rgb_image(self.reader, entry, res=res)
                    res_name = res
                    print(f"  Decoded Kodak USA raw RGB at: {res_name}  {pil_img.size}")
                    self.img_cache[cache_key] = (pil_img, res_name)
                else:
                    # Standard Photo CD Image Pack decode
                    # Look up per-image rotation from INFO.PCD Image Descriptors
                    rotation = 0
                    if self.disc_info and self.disc_info.get("image_descriptors"):
                        descs = self.disc_info["image_descriptors"]
                        if index < len(descs):
                            rotation = descs[index]["rotation"]

                    base_img = read_base_image(self.reader, entry, rotation=rotation)

                    if res == "Base" or not self.hires_decode_fn:
                        pil_img, res_name = base_img, "Base"
                    elif res == "4Base":
                        self.lbl_status.config(text=f"Decoding {name} at 4Base...")
                        self.root.update()
                        from photocd_hires import decode_4base
                        try:
                            fb = decode_4base(self.reader, entry, base_img)
                            pil_img, res_name = (fb, "4Base") if fb else (base_img, "Base")
                        except Exception as e:
                            print(f"  4Base error: {e}")
                            pil_img, res_name = base_img, "Base"
                    else:  # "16Base"
                        self.lbl_status.config(text=f"Decoding {name} at 16Base...")
                        self.root.update()
                        pil_img, res_name = self.hires_decode_fn(self.reader, entry, base_img)

                    print(f"  Decoded at: {res_name}")
                    self.img_cache[cache_key] = (pil_img, res_name)
            except Exception as e:
                self.lbl_status.config(text=f"Error decoding {name}: {e}")
                print(f"  Decode error: {e}")
                import traceback; traceback.print_exc()
                return

        pil_img, res_name = self.img_cache[cache_key]
        self._current_pil   = pil_img
        self._current_name  = name
        self._current_res   = res_name

        disc_prefix = f"{self._disc_title} — " if self._disc_title else ""
        self.root.title(f"Photo CD Player — {disc_prefix}{name}")

        w, h = pil_img.size
        self.lbl_status.config(text=f"{name}  •  {w}×{h} ({res_name})")
        self.lbl_count.config(text=f"{index + 1} / {len(self.images)}")

        self.btn_prev.config(state=tk.NORMAL if index > 0 else tk.DISABLED)
        self.btn_next.config(state=tk.NORMAL if index < len(self.images) - 1
                             else tk.DISABLED)
        self.btn_save.config(state=tk.NORMAL)

        self._highlight_thumb(index)
        self._redraw()

    def _on_canvas_resize(self, event):
        self._redraw()

    def _redraw(self):
        """Scale current image to fit canvas and redraw."""
        if not hasattr(self, '_current_pil') or self._current_pil is None:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        iw, ih = self._current_pil.size
        scale = min(cw / iw, ch / ih)
        dw = max(1, int(iw * scale))
        dh = max(1, int(ih * scale))
        display = self._current_pil.resize((dw, dh), Image.LANCZOS)
        self._tk_img = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img)

    def toggle_slideshow(self):
        if self.slideshow_on:
            self._stop_slideshow()
        else:
            self._start_slideshow()

    def _start_slideshow(self):
        self.slideshow_on = True
        if self.btn_slide:
            self.btn_slide.config(text="⏸")
        self._schedule_next()

    def _stop_slideshow(self):
        self.slideshow_on = False
        if self.btn_slide:
            self.btn_slide.config(text="▶")
        if self.slideshow_id:
            self.root.after_cancel(self.slideshow_id)
            self.slideshow_id = None

    def _schedule_next(self):
        if not self.slideshow_on:
            return
        # Get disc-specified time for current image (1-based image number)
        img_num = self.index + 1
        delay_s = self.image_timings.get(img_num, None)
        if delay_s is None:
            # No timing for this image — stop slideshow
            self._stop_slideshow()
            return
        self.slideshow_id = self.root.after(int(delay_s * 1000), self._slideshow_advance)

    def _slideshow_advance(self):
        if not self.slideshow_on:
            return
        if self.index < len(self.images) - 1:
            self.index += 1
        else:
            self.index = 0
        self._load_image(self.index)
        self._schedule_next()

    def save_image(self):
        """Save current image as PNG using the disc filename (IMG0001.PCD → IMG0001.png)."""
        entry    = self.images[self.index]
        lba      = entry["lba"]
        pcd_name = entry["name"]

        cache_key = (lba, self.res_var.get())
        if cache_key not in self.img_cache:
            self.lbl_status.config(text="Nothing to save — image not loaded yet.")
            return

        # Prompt for save location if not yet set
        save_dir = self.config.get("save_dir")
        if not save_dir:
            save_dir = self.set_save_dir()
            if not save_dir:
                return  # user cancelled

        base = pcd_name
        if base.upper().endswith(".PCD"):
            base = base[:-4]
        png_name = base + ".png"
        out_path = os.path.join(save_dir, png_name)

        try:
            pil_img, _ = self.img_cache[cache_key]
            pil_img.save(out_path)
            self.lbl_status.config(text=f"Saved  {png_name}  ✓")
            print(f"  Saved: {out_path}")
        except Exception as e:
            self.lbl_status.config(text=f"Save failed: {e}")
            print(f"  Save error: {e}")

    def _set_disc_loaded_ui(self, loaded):
        """Show/hide disc-specific controls based on whether a disc is loaded."""
        if loaded:
            # Pack in order: _disc_frame | Library | Open .cue | Fullscreen | Save .png | Settings
            # Must pack btn_library first (anchored to _open_cue_btn which is always packed),
            # then pack _disc_frame before btn_library.
            self.btn_library.pack(side=tk.LEFT, padx=2, before=self._open_cue_btn)
            self._disc_frame.pack(side=tk.LEFT, before=self.btn_library)
            self.btn_save.pack(side=tk.LEFT, padx=2, before=self.btn_settings)
        else:
            self._disc_frame.pack_forget()
            self.btn_library.pack_forget()
            self.btn_save.pack_forget()

    def _show_library(self):
        """Return to the library browser view."""
        self._set_disc_loaded_ui(False)
        if self.audio_player:
            self.audio_player.stop()
        self._lib_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._populate_library_list()
        self.root.title(APP_NAME)
        self.lbl_disc_title.config(text="")
        self.lbl_status.config(text="Double click a Library title or open a file to load a disc.")
        self.lbl_count.config(text="")

    def _populate_library_list(self):
        """Scan library_dir for .cue files and populate the listbox."""
        self._lib_listbox.delete(0, tk.END)
        self._lib_cue_paths = []
        lib_dir = self.config.get("library_dir")
        if not lib_dir or not os.path.isdir(lib_dir):
            self._lib_listbox.pack_forget()
            self._lib_msg.pack(expand=True)
            return
        entries = []
        for entry in sorted(os.scandir(lib_dir), key=lambda e: e.name.lower()):
            if not entry.is_dir():
                continue
            for f in os.scandir(entry.path):
                if f.name.lower().endswith(".cue"):
                    entries.append((entry.name, f.path))
                    break
        if not entries:
            self._lib_listbox.pack_forget()
            self._lib_msg.pack(expand=True)
            return
        self._lib_msg.pack_forget()
        self._lib_listbox.pack(fill=tk.BOTH, expand=True)
        for name, path in entries:
            self._lib_listbox.insert(tk.END, f"  {name}")
            self._lib_cue_paths.append(path)

    def _on_lib_select(self, event=None):
        sel = self._lib_listbox.curselection()
        if not sel or sel[0] >= len(self._lib_cue_paths):
            return
        path = self._lib_cue_paths[sel[0]]
        self._load_disc_from_path(path)

    def set_library_dir(self):
        """Let the user choose their disc library root directory and persist it."""
        initial = self.config.get("library_dir") or os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            title="Select disc library folder",
            initialdir=initial,
        )
        if not chosen:
            return
        self.config["library_dir"] = chosen
        save_config(self.config)
        self.lbl_status.config(text=f"Library set to: {chosen}")
        self._populate_library_list()

    def set_save_dir(self):
        """Let the user choose where PNG exports are saved. Returns chosen path or None."""
        initial = self.config.get("save_dir") or os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            title="Select folder for saved PNG images",
            initialdir=initial,
        )
        if not chosen:
            return None
        self.config["save_dir"] = chosen
        save_config(self.config)
        self.lbl_status.config(text=f"Save location set to: {chosen}")
        return chosen

    def open_cue(self):
        """Open a disc image, PCD image pack, or raw RGB file via file picker dialog."""
        initial = self.config.get("library_dir") or os.path.expanduser("~")
        path = filedialog.askopenfilename(
            title="Open Photo CD file",
            initialdir=initial,
            filetypes=[
                ("Photo CD files", "*.cue *.pcd *.rgb"),
                ("CUE sheet", "*.cue"),
                ("Image Pack", "*.pcd"),
                ("Raw RGB", "*.rgb"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        # Remember the directory we just opened from
        chosen_dir = os.path.dirname(os.path.dirname(path))
        if os.path.isdir(chosen_dir):
            self.config["library_dir"] = chosen_dir
            save_config(self.config)

        self._load_disc_from_path(path)

    def _load_disc_from_path(self, path):
        self.lbl_status.config(text=f"Opening {os.path.basename(path)}...")
        self.root.update()

        ext = os.path.splitext(path)[1].upper()

        # Standalone Image Pack (.PCD) or raw RGB (.RGB) — wrap in a virtual disc
        if ext in (".PCD", ".RGB"):
            try:
                reader, images, audio_tracks, audio_start_s, max_resolution, disc_info = \
                    _open_standalone_file(path)
            except Exception as e:
                self.lbl_status.config(text=f"Failed to open file: {e}")
                return
        else:
            try:
                reader, images, audio_tracks, audio_start_s, max_resolution, disc_info = \
                    open_disc(path)
            except Exception as e:
                self.lbl_status.config(text=f"Failed to open disc: {e}")
                return

        if not images:
            self.lbl_status.config(text="No images found on disc.")
            return

        # Hide library browser
        self._lib_frame.place_forget()

        # Tear down old state
        if self.audio_player:
            self.audio_player.stop()
        if self.reader:
            self.reader.close()

        self.reader       = reader
        self.images       = images
        self.index        = 0
        self.img_cache    = {}
        self.disc_info    = disc_info
        self._current_pil = None
        self.audio_player = AudioPlayer(audio_tracks, start_s=audio_start_s)

        # Clear thumbnail sidebar
        for w in self._thumb_inner.winfo_children():
            w.destroy()
        self._thumb_labels = []
        self.image_timings = disc_info.get("image_timings", {}) if disc_info else {}
        self._stop_slideshow()

        if max_resolution >= 1:
            try:
                from photocd_hires import read_highest_resolution
                self.hires_decode_fn = read_highest_resolution
            except ImportError:
                self.hires_decode_fn = None
        else:
            self.hires_decode_fn = None

        # Update resolution selector
        self.max_resolution = max_resolution
        choices = self._res_choices(max_resolution)
        menu = self.res_menu["menu"]
        menu.delete(0, "end")
        for c in choices:
            menu.add_command(label=c,
                             command=tk._setit(self.res_var, c, self._on_res_change))
        self.res_var.set(self._default_res(max_resolution))

        # Update disc title
        self._disc_title = self._make_disc_title(disc_info)
        title = f"Photo CD Player — {self._disc_title}" if self._disc_title else APP_NAME
        self.root.title(title)

        # Refresh top-bar disc title label and image count
        self.lbl_disc_title.config(text=self._disc_title or "")
        self.lbl_count.config(text=f"1 / {len(self.images)}")

        self._set_disc_loaded_ui(True)
        self._load_image(0)
        self.root.after(500, self.audio_player.play)
        if self.image_timings:
            self.root.after(600, self._start_slideshow)
        if self._filmstrip_visible:
            self.root.after(100, self._load_thumbnails_async)

    def prev_image(self):
        if self.index > 0:
            self.index -= 1
            self._load_image(self.index)

    def next_image(self):
        if self.index < len(self.images) - 1:
            self.index += 1
            self._load_image(self.index)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Photo CD Image Viewer")
    parser.add_argument("cue", nargs="?", help="Path to .cue file (optional; use Open .cue in GUI)")
    parser.add_argument("image", nargs="?", type=int, default=1,
                        help="Image number to display first (default: 1)")
    args = parser.parse_args()

    reader = None
    images = []
    audio_player = AudioPlayer([], start_s=0)
    hires_decode_fn = None
    disc_info = None
    max_resolution = 0
    start = 0

    if args.cue:
        if not os.path.isfile(args.cue):
            print(f"Error: not found: {args.cue}")
            sys.exit(1)

        print("\nOpening disc...")
        reader, images, audio_tracks, audio_start_s, max_resolution, disc_info = open_disc(args.cue)

        if not images:
            print("No images found on disc.")
            sys.exit(1)

        print(f"Found {len(images)} images. Opening viewer at image {args.image}...\n")
        start = max(0, min(args.image - 1, len(images) - 1))

        audio_player = AudioPlayer(audio_tracks, start_s=audio_start_s)

        if max_resolution >= 1:
            try:
                from photocd_hires import read_highest_resolution
                hires_decode_fn = read_highest_resolution
            except ImportError as e:
                print(f"  WARNING: Could not load hires decoder: {e}")

    root = tk.Tk()

    # Fix HiDPI/Retina scaling for PyInstaller bundles on macOS.
    # The bundled Tk defaults to 72 DPI; read the actual screen DPI and apply it.
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        try:
            dpi = root.winfo_fpixels("1i")
            root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    app  = PhotoCDViewer(root, reader, images,
                         audio_player=audio_player,
                         image_timings=None,
                         hires_decode_fn=hires_decode_fn,
                         disc_info=disc_info,
                         start_index=start,
                         max_resolution=max_resolution)
    root.mainloop()
    if reader:
        reader.close()
    audio_player.stop()
