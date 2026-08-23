from __future__ import annotations

import struct
import zlib


def png_dhash(payload: bytes) -> str:
    """Compute a 64-bit dHash for ordinary 8-bit Android PNG frames.

    The implementation intentionally uses only the standard library so the
    normal launcher does not gain an image-library dependency.
    """

    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    position = 8
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


def _paeth(left: int, up: int, upper_left: int) -> int:
    prediction = left + up - upper_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    return up if up_distance <= upper_left_distance else upper_left
