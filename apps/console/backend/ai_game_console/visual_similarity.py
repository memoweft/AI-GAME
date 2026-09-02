from __future__ import annotations

import struct
import zlib


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_dhash(payload: bytes) -> str:
    """Compute a 64-bit dHash for ordinary 8-bit Android PNG frames.

    The implementation intentionally uses only the standard library so the
    normal launcher does not gain an image-library dependency.
    """

    width, height, color_type, channels, rows = _decode_png(payload)
    samples: list[int] = []
    for sample_y in range(8):
        row = rows[min(height - 1, int((sample_y + 0.5) * height / 8))]
        for sample_x in range(9):
            pixel = min(width - 1, int((sample_x + 0.5) * width / 9)) * channels
            if color_type in {0, 4}:
                luminance = row[pixel]
            else:
                luminance = (
                    299 * row[pixel]
                    + 587 * row[pixel + 1]
                    + 114 * row[pixel + 2]
                ) // 1000
            samples.append(luminance)
    bits = 0
    for row_index in range(8):
        for column in range(8):
            start = row_index * 9 + column
            bits = (bits << 1) | int(samples[start] > samples[start + 1])
    return f"{bits:016x}"


def fingerprint_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        return max(len(left), len(right)) * 4
    return sum((int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(left, right))


def png_perceptual_distance(left: bytes, right: bytes) -> int | None:
    """Return dHash distance, or None when either frame cannot be decoded."""

    try:
        return fingerprint_distance(png_dhash(left), png_dhash(right))
    except (ValueError, zlib.error, struct.error):
        return None


def png_sampled_change_ratio(
    left: bytes,
    right: bytes,
    *,
    sample_columns: int = 32,
    sample_rows: int = 32,
    channel_threshold: int = 24,
) -> float | None:
    """Return the share of sampled pixels with a material channel change.

    dHash deliberately compresses a frame to 64 relative-luminance bits.  Two
    large dark Android pages can therefore collide even when their controls and
    layout are visibly different.  This denser absolute-color probe is a second
    signal for the narrow question "is this frame materially unchanged?".
    """

    if sample_columns < 1 or sample_rows < 1:
        raise ValueError("sample grid must be positive")
    if not 0 <= channel_threshold <= 255:
        raise ValueError("channel threshold must be between 0 and 255")
    try:
        left_frame = _decode_png(left)
        right_frame = _decode_png(right)
    except (ValueError, zlib.error, struct.error):
        return None
    left_width, left_height, left_type, left_channels, left_rows = left_frame
    right_width, right_height, right_type, right_channels, right_rows = right_frame
    if left_width != right_width or left_height != right_height:
        return 1.0

    changed = 0
    total = sample_columns * sample_rows
    for sample_y in range(sample_rows):
        y = min(left_height - 1, int((sample_y + 0.5) * left_height / sample_rows))
        for sample_x in range(sample_columns):
            x = min(left_width - 1, int((sample_x + 0.5) * left_width / sample_columns))
            left_pixel = _rgb(left_rows[y], x, left_type, left_channels)
            right_pixel = _rgb(right_rows[y], x, right_type, right_channels)
            if max(abs(a - b) for a, b in zip(left_pixel, right_pixel, strict=True)) >= channel_threshold:
                changed += 1
    return changed / total


def _decode_png(payload: bytes) -> tuple[int, int, int, int, list[bytearray]]:
    if not payload.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    position = len(_PNG_SIGNATURE)
    width = height = color_type = bit_depth = 0
    compressed = bytearray()
    while position + 12 <= len(payload):
        length = struct.unpack(">I", payload[position:position + 4])[0]
        chunk_type = payload[position + 4:position + 8]
        data = payload[position + 8:position + 8 + length]
        position += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        elif chunk_type == b"IDAT":
            compressed.extend(data)
        elif chunk_type == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if not width or not height or bit_depth != 8 or channels is None:
        raise ValueError("unsupported PNG format")
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    if len(raw) != (stride + 1) * height:
        raise ValueError("invalid PNG scanline size")
    rows: list[bytearray] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        current = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += stride + 1
        for index in range(stride):
            left = current[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                current[index] = (current[index] + left) & 255
            elif filter_type == 2:
                current[index] = (current[index] + up) & 255
            elif filter_type == 3:
                current[index] = (current[index] + ((left + up) // 2)) & 255
            elif filter_type == 4:
                current[index] = (current[index] + _paeth(left, up, upper_left)) & 255
            elif filter_type != 0:
                raise ValueError("unsupported PNG filter")
        rows.append(current)
        previous = current
    return width, height, color_type, channels, rows


def _rgb(row: bytearray, x: int, color_type: int, channels: int) -> tuple[int, int, int]:
    offset = x * channels
    if color_type in {0, 4}:
        value = row[offset]
        return value, value, value
    return row[offset], row[offset + 1], row[offset + 2]


def _paeth(left: int, up: int, upper_left: int) -> int:
    prediction = left + up - upper_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    return up if up_distance <= upper_left_distance else upper_left
