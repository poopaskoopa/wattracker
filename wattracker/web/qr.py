"""Small dependency-free QR encoder for the short desktop pairing payload."""
from __future__ import annotations

import html

_SIZE = 21
_DATA_CODEWORDS = 19
_EC_CODEWORDS = 7
_QUIET_ZONE = 4


def _gf_mul(left: int, right: int) -> int:
    result = 0
    while right:
        if right & 1:
            result ^= left
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
        right >>= 1
    return result & 0xFF


def _generator() -> list[int]:
    polynomial = [1]
    root = 1
    for _ in range(_EC_CODEWORDS):
        expanded = [0] * (len(polynomial) + 1)
        for index, coefficient in enumerate(polynomial):
            expanded[index] ^= coefficient
            expanded[index + 1] ^= _gf_mul(coefficient, root)
        polynomial = expanded
        root = _gf_mul(root, 2)
    return polynomial


_GENERATOR = _generator()


def _format_bits(mask: int) -> int:
    value = (0b01 << 3) | mask  # error correction level L
    remainder = value << 10
    generator = 0x537
    while remainder.bit_length() >= generator.bit_length():
        remainder ^= generator << (remainder.bit_length() - generator.bit_length())
    return ((value << 10) | remainder) ^ 0x5412


def _data_codewords(payload: bytes) -> list[int]:
    if len(payload) > 17:  # version 1-L byte-mode capacity
        raise ValueError("pairing payload is too long for the QR encoder")
    bits: list[int] = [0, 1, 0, 0]
    bits.extend((len(payload) >> shift) & 1 for shift in range(7, -1, -1))
    for byte in payload:
        bits.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
    bits.extend([0] * min(4, _DATA_CODEWORDS * 8 - len(bits)))
    bits.extend([0] * ((-len(bits)) % 8))
    pad = (0xEC, 0x11)
    index = 0
    while len(bits) < _DATA_CODEWORDS * 8:
        byte = pad[index % 2]
        bits.extend((byte >> shift) & 1 for shift in range(7, -1, -1))
        index += 1
    return [
        sum(bits[start + offset] << (7 - offset) for offset in range(8))
        for start in range(0, len(bits), 8)
    ]


def _error_codewords(data: list[int]) -> list[int]:
    remainder = [0] * _EC_CODEWORDS
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for index in range(_EC_CODEWORDS):
            remainder[index] ^= _gf_mul(_GENERATOR[index + 1], factor)
    return remainder


def _finder(matrix: list[list[int | None]], row: int, column: int) -> None:
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            target_row, target_column = row + dr, column + dc
            if not (0 <= target_row < _SIZE and 0 <= target_column < _SIZE):
                continue
            if 0 <= dr <= 6 and 0 <= dc <= 6:
                black = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                matrix[target_row][target_column] = 1 if black else 0
            else:
                matrix[target_row][target_column] = 0


def _function_matrix() -> list[list[int | None]]:
    matrix: list[list[int | None]] = [[None] * _SIZE for _ in range(_SIZE)]
    _finder(matrix, 0, 0)
    _finder(matrix, 0, _SIZE - 7)
    _finder(matrix, _SIZE - 7, 0)
    for index in range(8, _SIZE - 8):
        if matrix[6][index] is None:
            matrix[6][index] = 1 - (index % 2)
        if matrix[index][6] is None:
            matrix[index][6] = 1 - (index % 2)
    for index in range(15):
        if index < 6:
            row = index
        elif index < 8:
            row = index + 1
        else:
            row = _SIZE - 15 + index
        matrix[row][8] = 0
        if index < 8:
            column = _SIZE - index - 1
        elif index < 9:
            column = 15 - index
        else:
            column = 15 - index - 1
        matrix[8][column] = 0
    matrix[_SIZE - 8][8] = 1
    return matrix


def _mask(mask: int, row: int, column: int) -> bool:
    if mask == 0:
        return (row + column) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return column % 3 == 0
    if mask == 3:
        return (row + column) % 3 == 0
    if mask == 4:
        return (row // 2 + column // 3) % 2 == 0
    if mask == 5:
        product = row * column
        return product % 2 + product % 3 == 0
    if mask == 6:
        product = row * column
        return (product % 2 + product % 3) % 2 == 0
    if mask == 7:
        return ((row + column) % 2 + (row * column) % 3) % 2 == 0
    raise ValueError("QR mask is invalid")


def _matrix(payload: bytes, mask: int = 0) -> list[list[int]]:
    data = _data_codewords(payload)
    codewords = data + _error_codewords(data)
    bits = [
        (byte >> shift) & 1
        for byte in codewords
        for shift in range(7, -1, -1)
    ]
    matrix = _function_matrix()
    bit_index = 0
    upward = True
    column = _SIZE - 1
    while column > 0:
        if column == 6:
            column -= 1
        rows = range(_SIZE - 1, -1, -1) if upward else range(_SIZE)
        for row in rows:
            for current_column in (column, column - 1):
                if matrix[row][current_column] is not None:
                    continue
                bit = bits[bit_index] if bit_index < len(bits) else 0
                bit_index += 1
                matrix[row][current_column] = bit ^ _mask(mask, row, current_column)
        upward = not upward
        column -= 2
    format_value = _format_bits(mask)
    for index in range(15):
        bit = (format_value >> index) & 1
        if index < 6:
            row = index
        elif index < 8:
            row = index + 1
        else:
            row = _SIZE - 15 + index
        matrix[row][8] = bit
        if index < 8:
            current_column = _SIZE - index - 1
        elif index < 9:
            current_column = 15 - index
        else:
            current_column = 15 - index - 1
        matrix[8][current_column] = bit
    matrix[_SIZE - 8][8] = 1
    return [[int(value or 0) for value in row] for row in matrix]


def pairing_qr_svg(value: str) -> str | None:
    """Return a scannable version 1-L QR SVG for a short pairing code."""
    if not isinstance(value, str) or not value or len(value) > 17:
        return None
    try:
        payload = value.encode("ascii")
    except UnicodeEncodeError:
        return None
    matrix = _matrix(payload)
    total = _SIZE + _QUIET_ZONE * 2
    modules: list[str] = []
    for row, values in enumerate(matrix):
        for column, bit in enumerate(values):
            if bit:
                modules.append(
                    f"M{column + _QUIET_ZONE},{row + _QUIET_ZONE}h1v1h-1z"
                )
    label = html.escape(f"Pairing code {value}", quote=True)
    return (
        f'<svg class="cloud-qr-svg" viewBox="0 0 {total} {total}" '
        f'role="img" aria-label="{label}" focusable="false" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{total}" height="{total}" fill="white"/>'
        f'<path d="{"".join(modules)}" fill="black" '
        'shape-rendering="crispEdges"/></svg>'
    )
