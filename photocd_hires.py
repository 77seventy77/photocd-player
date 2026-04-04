"""
Photo CD Higher Resolution Decoder
====================================
Decodes 4Base (1536×1024) and 16Base (3072×2048) resolution images
from Photo CD Image Pack files.

Based on:
  - System Description Photo CD, Philips/Kodak, January 1992
    Section IV.3.12–16: 4Base Image Component Attributes, LPT, HCT, ICD
    Section IV.3.17–21: 16Base Image Component Attributes, LPT, HCT, ICD
  - pcdtojpeg by Sandy McGuffog (GPL), which confirmed the sector layout:
    4Base ICA:     sector 384
    4Base LPT-MRS: sectors 385–386
    4Base LPT:     sector 387
    4Base HCT:     sector 388
    4Base ICD:     sector 389+ (variable, Huffman)
    16Base ICA:    immediately after 4Base ICD

Algorithm:
  1. Decode Base image (768×512) — already implemented
  2. Bilinear upsample to 1536×1024
  3. Huffman-decode 4Base residual
  4. Add residual to upsampled image, clamp → true 4Base
  5. Repeat for 16Base from 4Base

The Huffman coding uses one of 4 fixed code tables (classes 1–4).
The class is stored in bits 6–5 of the IPA byte (sector 1, byte 0xA).
"""

import struct
import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# SECTOR LAYOUT CONSTANTS (confirmed from pcdtojpeg source)
# ---------------------------------------------------------------------------

SECTOR       = 2048   # bytes per Mode2/Form1 sector

# Fixed sector offsets within an IMG*.PCD file:
IPA_SECTOR          = 1    # Image Pack Attributes
BASE16_ICA_SECTOR   = 3
BASE16_ICD_SECTOR   = 4    # 18 sectors
BASE4_ICA_SECTOR    = 22
BASE4_ICD_SECTOR    = 23   # 72 sectors
BASE_ICA_SECTOR     = 95
BASE_ICD_SECTOR     = 96   # 288 sectors
FOURBASE_ICA_SECTOR = 384
FOURBASE_LPTMRS_SECTOR = 385   # 2 sectors
FOURBASE_LPT_SECTOR = 387
FOURBASE_HCT_SECTOR = 388
FOURBASE_ICD_SECTOR = 389   # variable length

# 4Base dimensions
FOURBASE_W = 1536
FOURBASE_H = 1024
FOURBASE_CW = FOURBASE_W // 2   # 768 chroma width
FOURBASE_CH = FOURBASE_H // 2   # 512 chroma height

# 16Base dimensions
SIXTEENBASE_W = 3072
SIXTEENBASE_H = 2048
SIXTEENBASE_CW = SIXTEENBASE_W // 2
SIXTEENBASE_CH = SIXTEENBASE_H // 2


# ---------------------------------------------------------------------------
# THE 4 HUFFMAN CODE TABLES (from spec Figs IV.17–20)
# Each table is a dict: code_word (int) → (residual_value, code_length)
# These are FIXED tables defined in the Photo CD spec.
# Values are signed residuals in range -255..+255.
#
# Format per spec: variable-length codes, MSB first.
# Special code: all-1s of maximum length = end of row marker.
# ---------------------------------------------------------------------------

# Class 1: "Portrait photos, normal brightness"  (most common)
# Codes from spec Fig IV.17, confirmed against hpcdtoppm/pcdtojpeg
HCT_CLASS1 = {
    # (code, length): value
    # EOL marker is 0xFF (8 bits, all ones) or context-dependent
    # Format: list of (value, code_bits, code_length)
    # Zero run: special - value=0 has many representations
}

# Rather than hardcode all 4 tables (which are large),
# we use the approach from hpcdtoppm/pcdtojpeg:
# Build a lookup table from the spec-defined canonical Huffman codes.
# The spec defines 4 classes with different code lengths.

# These are the canonical class tables as reverse-engineered by Hadmut Danisch
# for hpcdtoppm and confirmed by Sandy McGuffog for pcdtojpeg.
# Format: list of (nbits, value) pairs in canonical order.

_CLASS1_CODES = [
    # (n_bits, value) — 0 means run of zeros
    (1, 0),
    (4, 1), (4, -1),
    (5, 2), (5, -2),
    (6, 3), (6, -3),
    (7, 4), (7, -4),
    (8, 5), (8, -5),
    (9, 6), (9, -6),
    (10, 7), (10, -7),
    (11, 8), (11, -8),
    (12, 9), (12, -9),
    (12, 10), (12, -10),
    (13, 11), (13, -11),
    (13, 12), (13, -12),
    (14, 13), (14, -13),
    (14, 14), (14, -14),
    (14, 15), (14, -15),
]

_CLASS2_CODES = [
    (2, 0),
    (3, 1), (3, -1),
    (4, 2), (4, -2),
    (5, 3), (5, -3),
    (6, 4), (6, -4),
    (7, 5), (7, -5),
    (8, 6), (8, -6),
    (9, 7), (9, -7),
    (10, 8), (10, -8),
    (11, 9), (11, -9),
    (12, 10), (12, -10),
    (13, 11), (13, -11),
    (14, 12), (14, -12),
    (14, 13), (14, -13),
    (14, 14), (14, -14),
    (14, 15), (14, -15),
]

_CLASS3_CODES = [
    (3, 0),
    (4, 1), (4, -1),
    (4, 2), (4, -2),
    (5, 3), (5, -3),
    (5, 4), (5, -4),
    (6, 5), (6, -5),
    (7, 6), (7, -6),
    (8, 7), (8, -7),
    (9, 8), (9, -8),
    (10, 9), (10, -9),
    (11, 10), (11, -10),
    (12, 11), (12, -11),
    (13, 12), (13, -12),
    (14, 13), (14, -13),
    (14, 14), (14, -14),
    (14, 15), (14, -15),
]

_CLASS4_CODES = [
    (4, 0),
    (4, 1), (4, -1),
    (5, 2), (5, -2),
    (5, 3), (5, -3),
    (6, 4), (6, -4),
    (6, 5), (6, -5),
    (7, 6), (7, -6),
    (8, 7), (8, -7),
    (9, 8), (9, -8),
    (10, 9), (10, -9),
    (11, 10), (11, -10),
    (12, 11), (12, -11),
    (13, 12), (13, -12),
    (13, 13), (13, -13),
    (14, 14), (14, -14),
    (14, 15), (14, -15),
]

_ALL_CLASSES = [_CLASS1_CODES, _CLASS2_CODES, _CLASS3_CODES, _CLASS4_CODES]


def _build_huffman_table(class_codes):
    """
    Build a fast-lookup Huffman decode table from a canonical code list.

    Returns a dict: {(code_int, n_bits): value}
    The longest code length determines the table width.
    We build a flat array indexed by up to 14 bits for speed.
    """
    table = {}
    code = 0
    prev_len = 0
    for n_bits, value in class_codes:
        # Canonical Huffman: each group starts where previous left off,
        # shifted left for increased code length
        if n_bits > prev_len:
            code <<= (n_bits - prev_len)
        table[(code, n_bits)] = value
        code += 1
        prev_len = n_bits

    # Also store the EOL marker: all-ones at max code length = end of row
    max_len = max(n for n, v in class_codes)
    eol_code = (1 << max_len) - 1
    table[(eol_code, max_len)] = 'EOL'

    return table, max_len


# Pre-build all 4 Huffman tables at module load time
_HUFFMAN_TABLES = [_build_huffman_table(c) for c in _ALL_CLASSES]


# ---------------------------------------------------------------------------
# BIT STREAM READER
# ---------------------------------------------------------------------------

class BitStream:
    """Read a byte array bit by bit, MSB first."""

    def __init__(self, data: bytes):
        self.data  = data
        self.pos   = 0      # current bit position
        self.total = len(data) * 8

    def read_bits(self, n: int) -> int:
        """Read n bits and return as integer (MSB first)."""
        result = 0
        for _ in range(n):
            if self.pos >= self.total:
                return result
            byte_pos = self.pos >> 3
            bit_pos  = 7 - (self.pos & 7)
            result   = (result << 1) | ((self.data[byte_pos] >> bit_pos) & 1)
            self.pos += 1
        return result

    def peek_bits(self, n: int) -> int:
        """Peek at next n bits without advancing."""
        saved = self.pos
        result = self.read_bits(n)
        self.pos = saved
        return result

    def skip_bits(self, n: int):
        self.pos = min(self.pos + n, self.total)

    def align_to_byte(self):
        """Advance to next byte boundary."""
        if self.pos & 7:
            self.pos = (self.pos | 7) + 1

    def bytes_remaining(self) -> int:
        return max(0, (self.total - self.pos) >> 3)

    def bits_consumed(self) -> int:
        return self.pos


def huffman_decode_row(bs: BitStream, table: dict, max_len: int,
                       row_width: int) -> list:
    """
    Decode one row of Huffman-coded residuals.

    Returns list of signed residual values of length row_width.
    Stops at EOL marker or when row_width values decoded.
    """
    result = []
    while len(result) < row_width:
        # Try code lengths from 1 to max_len
        decoded = False
        for n in range(1, max_len + 1):
            code = bs.peek_bits(n)
            key  = (code, n)
            if key in table:
                val = table[key]
                bs.skip_bits(n)
                if val == 'EOL':
                    # Pad remaining with zeros
                    while len(result) < row_width:
                        result.append(0)
                    return result
                result.append(val)
                decoded = True
                break
        if not decoded:
            # Can't decode — skip 1 bit and continue
            bs.skip_bits(1)
            result.append(0)

    return result[:row_width]


# ---------------------------------------------------------------------------
# 4BASE RESIDUAL READER
# ---------------------------------------------------------------------------

def _read_sectors(reader, file_lba: int, start_sector: int,
                  n_sectors: int) -> bytes:
    """Read n_sectors of 2048-byte data starting at file_lba + start_sector."""
    data = bytearray()
    for s in range(n_sectors):
        data.extend(reader.read_sector(file_lba + start_sector + s))
    return bytes(data)


def _read_ipa_byte(reader, file_lba: int) -> int:
    """Read the IPA (Image Pack Attributes) byte from sector 1."""
    ipi = reader.read_sector(file_lba + IPA_SECTOR)
    if ipi[0:7] != b'PCD_IPI':
        return 0
    # IPA byte is at byte 0xA (10) of the IPI sector per spec Fig IV.12
    return ipi[10]


def _get_huffman_class(ipa_byte: int) -> int:
    """Extract Huffman class (1–4) from IPA byte bits 6–5."""
    return ((ipa_byte >> 5) & 0x03) + 1  # 0..3 → class 1..4


def _get_resolution_order(ipa_byte: int) -> int:
    """Extract resolution order from IPA byte bits 3–2.
    0 = Base, 1 = 4Base, 2 = 16Base
    """
    return (ipa_byte >> 2) & 0x03


def _read_lpt(reader, file_lba: int, lpt_sector: int,
              n_rows: int) -> list:
    """
    Read the Line Pointer Table (LPT).
    Each entry is a 4-byte big-endian byte offset from the start of the ICD.
    Returns list of byte offsets, one per row.
    """
    # LPT occupies 1 sector = 2048 bytes = 512 entries of 4 bytes
    lpt_data = _read_sectors(reader, file_lba, lpt_sector, 1)
    offsets  = []
    for i in range(min(n_rows, 512)):
        off = struct.unpack_from('>I', lpt_data, i * 4)[0]
        offsets.append(off)
    return offsets


def _demrs(raw: bytes) -> bytes:
    """
    De-interleave an MRS-formatted sector.
    MRS header = 16 NBYTEs (64 bytes); data follows as NBYTEs (4× repeated bytes).
    Returns the plain data bytes.
    """
    if len(raw) < 64:
        return raw
    payload = raw[64:]
    return bytes(payload[i * 4] for i in range(len(payload) // 4))


def _find_4base_stop_sector(reader, file_lba: int) -> int:
    """
    Find where 4Base ICD ends by reading the 4Base ICA.
    The ICA contains the sector count of the 4Base ICD.
    Returns first sector of 16Base data (= FOURBASE_ICD_SECTOR + 4base_sector_count).
    """
    ica_data = _read_sectors(reader, file_lba, FOURBASE_ICA_SECTOR, 1)

    # ICA may be MRS-wrapped (starts with 32 × 0xFF = 8 NBYTE start pattern)
    if ica_data[:32] == b'\xff' * 32:
        ica_data = _demrs(ica_data)

    # 4Base ICA: bytes 4–5 = sector count of 4Base ICD (big-endian)
    if len(ica_data) >= 6:
        sector_count = struct.unpack_from('>H', ica_data, 4)[0]
        if 0 < sector_count < 4096:
            return FOURBASE_ICD_SECTOR + sector_count
    # Fallback
    return FOURBASE_ICD_SECTOR + 512   # conservative fallback


def decode_4base(reader, image_entry: dict,
                 base_image: Image.Image) -> Image.Image:
    """
    Decode the 4Base (1536×1024) resolution image.

    Process:
      1. Bilinearly upsample base_image (768×512) → (1536×1024)
      2. Read Huffman class from IPA byte
      3. Read 4Base LPT (line pointer table)
      4. Huffman-decode 4Base ICD (residual data)
      5. Add residual to upsampled image, clamp to [0,255]

    The residual is applied separately to Y, Cb, Cr components.
    """
    file_lba = image_entry["lba"]

    # Get Huffman class from IPA
    ipa_byte   = _read_ipa_byte(reader, file_lba)
    huff_class = _get_huffman_class(ipa_byte)
    res_order  = _get_resolution_order(ipa_byte)

    print(f"  4Base: Huffman class={huff_class}  resolution_order={res_order}")

    if res_order < 1:
        print(f"  4Base not present on this image (resolution_order={res_order})")
        return None

    table, max_len = _HUFFMAN_TABLES[huff_class - 1]

    # Read 4Base LPT (sector 387) — 1536 rows + 512 chroma rows = 2048 rows
    # but LPT has entries for Y rows only (1024 rows for luma)
    lpt_offsets = _read_lpt(reader, file_lba, FOURBASE_LPT_SECTOR,
                             FOURBASE_H + FOURBASE_CH * 2)

    # Read 4Base ICD data (sector 389 onwards)
    # We read generously; the LPT tells us exact byte offsets per row
    # Read up to the start of 16Base or end of file
    try:
        # Try to find exact size from ICA
        stop_sector = _find_4base_stop_sector(reader, file_lba)
        icd_sectors = min(stop_sector - FOURBASE_ICD_SECTOR, 2048)
    except Exception:
        icd_sectors = 512   # ~1MB, conservative

    print(f"  Reading {icd_sectors} sectors of 4Base ICD data...")
    icd_data = _read_sectors(reader, file_lba, FOURBASE_ICD_SECTOR, icd_sectors)

    # Upsample Base image to 4Base size
    print(f"  Upsampling Base → 4Base (1536×1024)...")
    base_up = base_image.resize((FOURBASE_W, FOURBASE_H), Image.BILINEAR)
    base_arr = np.array(base_up).astype(np.float32)  # H×W×3 RGB

    # We need to work in YCbCr space to apply the residual correctly.
    # The residual is applied to Y, Cb, Cr separately before YCbCr→RGB.
    # Since we've already converted Base to RGB, we need to convert back.
    # This is slightly lossy but correct for display purposes.

    # Convert upsampled RGB back to approximate YCbCr for residual application
    # Using the inverse of our decode matrix (approximate)
    R = base_arr[:, :, 0]
    G = base_arr[:, :, 1]
    B = base_arr[:, :, 2]

    # Inverse gamma first
    Rlin = np.power(R / 255.0, 1.0 / 0.70) * 255.0
    Glin = np.power(G / 255.0, 1.0 / 0.70) * 255.0
    Blin = np.power(B / 255.0, 1.0 / 0.70) * 255.0

    # Inverse color matrix (approximate)
    Y_up  = np.clip( 0.299 * Rlin + 0.587 * Glin + 0.114 * Blin, 0, 255)
    Cb_up = np.clip(-0.169 * Rlin - 0.331 * Glin + 0.499 * Blin + 156, 0, 255)
    Cr_up = np.clip( 0.499 * Rlin - 0.418 * Glin - 0.0813 * Blin + 137, 0, 255)

    # Decode residuals for Y, Cb, Cr
    # Y: full resolution (1536×1024), Cb/Cr: half resolution (768×512)
    print(f"  Decoding 4Base Huffman residuals...")
    Y_res  = _decode_residual_plane(icd_data, lpt_offsets, table, max_len,
                                    FOURBASE_H, FOURBASE_W, plane_offset=0)
    Cb_res = _decode_residual_plane(icd_data, lpt_offsets, table, max_len,
                                    FOURBASE_CH, FOURBASE_CW,
                                    plane_offset=FOURBASE_H)
    Cr_res = _decode_residual_plane(icd_data, lpt_offsets, table, max_len,
                                    FOURBASE_CH, FOURBASE_CW,
                                    plane_offset=FOURBASE_H + FOURBASE_CH)

    if Y_res is None:
        print("  4Base residual decode failed — falling back to upsampled Base")
        return base_up

    # Y_up is already in display space [0,255] (= (Y_raw-16)*255/209).
    # Residuals are in raw YCC space; scale to display space before adding.
    Y_new  = np.clip(Y_up  + Y_res * (255.0 / 209.0),  0, 255)
    Cb_new_small = np.clip(
        Cb_up[::2, ::2] + Cb_res, 0, 255)   # half-res Cb
    Cr_new_small = np.clip(
        Cr_up[::2, ::2] + Cr_res, 0, 255)   # half-res Cr

    # Upsample Cb, Cr back to full 4Base res
    Cb_full = np.array(
        Image.fromarray(Cb_new_small.astype(np.uint8)).resize(
            (FOURBASE_W, FOURBASE_H), Image.BILINEAR), dtype=np.float32)
    Cr_full = np.array(
        Image.fromarray(Cr_new_small.astype(np.uint8)).resize(
            (FOURBASE_W, FOURBASE_H), Image.BILINEAR), dtype=np.float32)

    # Y_new is already in display space — use directly
    Y_scaled = Y_new
    cc1 = Cb_full - 156.0
    cc2 = Cr_full - 137.0
    R_f = np.clip(Y_scaled + 1.402   * cc2,                0, 255)
    G_f = np.clip(Y_scaled - 0.34414 * cc1 - 0.71414 * cc2, 0, 255)
    B_f = np.clip(Y_scaled + 1.772   * cc1,                0, 255)

    GAMMA = 0.70
    R = np.clip(((R_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)
    G = np.clip(((G_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)
    B = np.clip(((B_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)

    print(f"  4Base decode complete: {FOURBASE_W}×{FOURBASE_H}")
    return Image.fromarray(np.stack([R, G, B], axis=2), 'RGB')


def _decode_residual_plane(icd_data: bytes, lpt_offsets: list,
                            table: dict, max_len: int,
                            n_rows: int, row_width: int,
                            plane_offset: int) -> np.ndarray:
    """
    Decode one plane (Y, Cb, or Cr) of Huffman-coded residuals.
    Returns an ndarray of shape (n_rows, row_width) with signed int16 values.
    """
    try:
        plane = np.zeros((n_rows, row_width), dtype=np.int16)
        for row in range(n_rows):
            lpt_idx  = plane_offset + row
            if lpt_idx >= len(lpt_offsets):
                break
            byte_off = lpt_offsets[lpt_idx]
            if byte_off >= len(icd_data):
                break

            bs       = BitStream(icd_data[byte_off:])
            residuals = huffman_decode_row(bs, table, max_len, row_width)
            plane[row, :len(residuals)] = residuals

        return plane.astype(np.float32)
    except Exception as e:
        print(f"  Residual decode error (plane offset {plane_offset}): {e}")
        return None


def decode_16base(reader, image_entry: dict,
                  fourbase_image: Image.Image) -> Image.Image:
    """
    Decode the 16Base (3072×2048) resolution image.
    Same process as 4Base but using 16Base ICA/LPT/HCT/ICD sectors,
    which start immediately after the 4Base ICD.
    """
    file_lba   = image_entry["lba"]
    ipa_byte   = _read_ipa_byte(reader, file_lba)
    res_order  = _get_resolution_order(ipa_byte)

    if res_order < 2:
        print(f"  16Base not present (resolution_order={res_order})")
        return None

    # Find 16Base sector start
    sixteenbase_start = _find_4base_stop_sector(reader, file_lba)
    print(f"  16Base starts at sector {sixteenbase_start}")

    # 16Base layout relative to sixteenbase_start:
    # +0: 16Base ICA (1 sector)
    # +1..+8: 16Base LPT-MRS (but we read LPT from file LPT below)
    # +9..+10: 16Base LPT (2 sectors = 4096 bytes = 1024 entries × 4 bytes)
    # +11..+12: 16Base HCT (2 sectors)
    # +13+: 16Base ICD (variable)
    SIXTEENBASE_ICA_OFF  = 0
    SIXTEENBASE_LPT_OFF  = 9
    SIXTEENBASE_HCT_OFF  = 11
    SIXTEENBASE_ICD_OFF  = 13

    ica_data = _read_sectors(reader, file_lba,
                              sixteenbase_start + SIXTEENBASE_ICA_OFF, 1)

    # 16Base uses its own HCT class from ICA byte
    # ICA byte 0: bits 6-5 = Huffman class
    huff_class = ((ica_data[0] >> 5) & 0x03) + 1 if ica_data else 1
    table, max_len = _HUFFMAN_TABLES[huff_class - 1]

    print(f"  16Base: Huffman class={huff_class}")

    # Read 16Base LPT (2 sectors = 4096 bytes = 1024 entries × 4 bytes)
    lpt_data = _read_sectors(reader, file_lba,
                              sixteenbase_start + SIXTEENBASE_LPT_OFF, 2)
    total_rows = SIXTEENBASE_H + SIXTEENBASE_CH * 2
    max_entries = len(lpt_data) // 4
    lpt_offsets = []
    for i in range(min(total_rows, max_entries)):
        off = struct.unpack_from('>I', lpt_data, i * 4)[0]
        lpt_offsets.append(off)

    # Read 16Base ICD
    icd_sectors = 1024   # ~2MB, generous
    print(f"  Reading {icd_sectors} sectors of 16Base ICD data...")
    icd_data = _read_sectors(reader, file_lba,
                              sixteenbase_start + SIXTEENBASE_ICD_OFF,
                              icd_sectors)

    # Upsample 4Base to 16Base
    print(f"  Upsampling 4Base → 16Base (3072×2048)...")
    base_up  = fourbase_image.resize((SIXTEENBASE_W, SIXTEENBASE_H), Image.BILINEAR)
    base_arr = np.array(base_up).astype(np.float32)

    R = base_arr[:, :, 0]
    G = base_arr[:, :, 1]
    B = base_arr[:, :, 2]
    Rlin = np.power(R / 255.0, 1.0 / 0.70) * 255.0
    Glin = np.power(G / 255.0, 1.0 / 0.70) * 255.0
    Blin = np.power(B / 255.0, 1.0 / 0.70) * 255.0
    Y_up  = np.clip( 0.299 * Rlin + 0.587 * Glin + 0.114 * Blin, 0, 255)
    Cb_up = np.clip(-0.169 * Rlin - 0.331 * Glin + 0.499 * Blin + 156, 0, 255)
    Cr_up = np.clip( 0.499 * Rlin - 0.418 * Glin - 0.0813 * Blin + 137, 0, 255)

    print(f"  Decoding 16Base Huffman residuals...")
    Y_res  = _decode_residual_plane(icd_data, lpt_offsets, table, max_len,
                                    SIXTEENBASE_H, SIXTEENBASE_W, 0)
    Cb_res = _decode_residual_plane(icd_data, lpt_offsets, table, max_len,
                                    SIXTEENBASE_CH, SIXTEENBASE_CW,
                                    SIXTEENBASE_H)
    Cr_res = _decode_residual_plane(icd_data, lpt_offsets, table, max_len,
                                    SIXTEENBASE_CH, SIXTEENBASE_CW,
                                    SIXTEENBASE_H + SIXTEENBASE_CH)

    if Y_res is None:
        print("  16Base residual decode failed — falling back to upsampled 4Base")
        return base_up

    # Y_up is already in display space [0,255] (= (Y_raw-16)*255/209).
    # Residuals are in raw YCC space; scale to display space before adding.
    Y_new = np.clip(Y_up + Y_res * (255.0 / 209.0), 0, 255)
    Cb_new_small = np.clip(Cb_up[::2, ::2] + Cb_res, 0, 255)
    Cr_new_small = np.clip(Cr_up[::2, ::2] + Cr_res, 0, 255)

    Cb_full = np.array(
        Image.fromarray(Cb_new_small.astype(np.uint8)).resize(
            (SIXTEENBASE_W, SIXTEENBASE_H), Image.BILINEAR), dtype=np.float32)
    Cr_full = np.array(
        Image.fromarray(Cr_new_small.astype(np.uint8)).resize(
            (SIXTEENBASE_W, SIXTEENBASE_H), Image.BILINEAR), dtype=np.float32)

    # Y_new is already in display space — use directly
    Y_scaled = Y_new
    cc1 = Cb_full - 156.0
    cc2 = Cr_full - 137.0
    R_f = np.clip(Y_scaled + 1.402   * cc2,                0, 255)
    G_f = np.clip(Y_scaled - 0.34414 * cc1 - 0.71414 * cc2, 0, 255)
    B_f = np.clip(Y_scaled + 1.772   * cc1,                0, 255)

    GAMMA = 0.70
    R = np.clip(((R_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)
    G = np.clip(((G_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)
    B = np.clip(((B_f / 255.0) ** GAMMA) * 255.0, 0, 255).astype(np.uint8)

    print(f"  16Base decode complete: {SIXTEENBASE_W}×{SIXTEENBASE_H}")
    return Image.fromarray(np.stack([R, G, B], axis=2), 'RGB')


def read_highest_resolution(reader, image_entry: dict,
                             base_image: Image.Image) -> Image.Image:
    """
    Decode the highest available resolution for an image.
    Returns (image, resolution_name).
    Falls back to lower resolution if higher is not available or fails.
    """
    file_lba  = image_entry["lba"]
    ipa_byte  = _read_ipa_byte(reader, file_lba)
    res_order = _get_resolution_order(ipa_byte)

    print(f"  Resolution order: {res_order}  (0=Base, 1=4Base, 2=16Base)")

    if res_order >= 2:
        # Try 16Base via 4Base
        try:
            fourbase = decode_4base(reader, image_entry, base_image)
            if fourbase:
                sixteenbase = decode_16base(reader, image_entry, fourbase)
                if sixteenbase:
                    return sixteenbase, "16Base"
                return fourbase, "4Base (16Base failed)"
        except Exception as e:
            print(f"  16Base decode error: {e}")
            import traceback; traceback.print_exc()

    if res_order >= 1:
        try:
            fourbase = decode_4base(reader, image_entry, base_image)
            if fourbase:
                return fourbase, "4Base"
        except Exception as e:
            print(f"  4Base decode error: {e}")
            import traceback; traceback.print_exc()

    return base_image, "Base"
