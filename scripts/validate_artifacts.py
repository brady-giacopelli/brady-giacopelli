#!/usr/bin/env python3
"""Validate the three artifacts published by the upstream snk renderer."""

from __future__ import annotations

import argparse
import struct
import xml.etree.ElementTree as ET
from pathlib import Path


EXPECTED = (
    "github-contribution-grid-snake.svg",
    "github-contribution-grid-snake-dark.svg",
    "github-contribution-grid-snake.gif",
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validate_svg(path: Path) -> tuple[str, str]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing or empty SVG: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ValueError(f"invalid SVG: {path}: {error}") from error
    if _local_name(root.tag) != "svg":
        raise ValueError(f"{path} does not have an SVG root")
    view_box = root.attrib.get("viewBox", "")
    if len(view_box.split()) != 4:
        raise ValueError(f"{path} has no usable SVG viewBox")
    rects = [element for element in root.iter() if _local_name(element.tag) == "rect"]
    class_grid_rects = [
        element
        for element in rects
        if "c" in element.attrib.get("class", "").split()
    ]
    # The pinned upstream renderer identifies grid cells with class="c". Keep
    # a geometry fallback because SVG optimizers may remove or rewrite class
    # attributes while preserving the grid's x/y/rx/ry cell geometry.
    geometry_grid_rects = [
        element
        for element in rects
        if {"x", "y", "rx", "ry"}.issubset(element.attrib)
        and "width" not in element.attrib
        and "height" not in element.attrib
    ]
    candidates = [candidate for candidate in (class_grid_rects, geometry_grid_rects) if candidate]
    grid_rects = next(
        (
            candidate
            for candidate in candidates
            if len({element.attrib.get("y", "") for element in candidate}) == 7
            and len({
                (element.attrib.get("x", ""), element.attrib.get("y", ""))
                for element in candidate
            })
            == len(candidate)
        ),
        None,
    )
    if grid_rects is None:
        details = ", ".join(
            f"{len(candidate)} cells/{len({element.attrib.get('y', '') for element in candidate})} rows"
            for candidate in candidates
        ) or "no grid candidates"
        raise ValueError(f"{path} does not contain exactly seven logical contribution rows ({details})")
    rows = {element.attrib.get("y", "") for element in grid_rects}
    columns = {element.attrib.get("x", "") for element in grid_rects}
    geometry = "|".join(
        ",".join(element.attrib.get(key, "") for key in ("x", "y", "width", "height", "rx", "ry"))
        for element in grid_rects
    )
    return view_box, geometry


def _sub_blocks(data: bytes, offset: int) -> tuple[bytes, int]:
    payload = bytearray()
    while True:
        if offset >= len(data):
            raise ValueError("GIF has a truncated sub-block")
        size = data[offset]
        offset += 1
        if size == 0:
            return bytes(payload), offset
        if offset + size > len(data):
            raise ValueError("GIF has a truncated sub-block payload")
        payload.extend(data[offset : offset + size])
        offset += size


def _decode_lzw(data: bytes, minimum_code_size: int) -> list[int]:
    if not 2 <= minimum_code_size <= 8:
        raise ValueError("GIF has an invalid LZW minimum code size")
    clear = 1 << minimum_code_size
    end = clear + 1
    code_size = minimum_code_size + 1
    dictionary = {index: [index] for index in range(clear)}
    next_code = end + 1
    bits = [(value >> bit) & 1 for value in data for bit in range(8)]
    position = 0
    previous: list[int] | None = None
    decoded: list[int] = []
    while position + code_size <= len(bits):
        code = sum(bits[position + bit] << bit for bit in range(code_size))
        position += code_size
        if code == clear:
            code_size = minimum_code_size + 1
            dictionary = {index: [index] for index in range(clear)}
            next_code = end + 1
            previous = None
            continue
        if code == end:
            return decoded
        if code in dictionary:
            entry = dictionary[code]
        elif code == next_code and previous is not None:
            entry = previous + [previous[0]]
        else:
            raise ValueError("GIF contains an invalid LZW code")
        decoded.extend(entry)
        if previous is not None:
            dictionary[next_code] = previous + [entry[0]]
            next_code += 1
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1
        previous = entry
    raise ValueError("GIF LZW stream is missing its end code")


def validate_gif(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing or empty GIF: {path}")
    data = path.read_bytes()
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a") or data[-1] != 0x3B:
        raise ValueError(f"{path} is not a complete GIF")
    width, height = struct.unpack_from("<HH", data, 6)
    if not width or not height:
        raise ValueError(f"{path} has an empty canvas")
    packed = data[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    image_count = 0
    while offset < len(data):
        marker = data[offset]
        offset += 1
        if marker == 0x3B:
            break
        if marker == 0x21:
            if offset >= len(data):
                raise ValueError(f"{path} has a truncated extension")
            offset += 1
            _, offset = _sub_blocks(data, offset)
            continue
        if marker != 0x2C:
            raise ValueError(f"{path} contains an unknown GIF block 0x{marker:02x}")
        if offset + 9 > len(data):
            raise ValueError(f"{path} has a truncated image descriptor")
        frame_width, frame_height = struct.unpack_from("<HH", data, offset + 4)
        image_packed = data[offset + 8]
        offset += 9
        if not frame_width or not frame_height:
            raise ValueError(f"{path} contains an empty GIF frame")
        if image_packed & 0x80:
            offset += 3 * (2 ** ((image_packed & 0x07) + 1))
        if offset >= len(data):
            raise ValueError(f"{path} has no GIF image data")
        minimum_code_size = data[offset]
        compressed, offset = _sub_blocks(data, offset + 1)
        decoded = _decode_lzw(compressed, minimum_code_size)
        if len(decoded) < frame_width * frame_height:
            raise ValueError(f"{path} contains a truncated decoded GIF frame")
        image_count += 1
    if image_count < 2 or b"NETSCAPE2.0" not in data:
        raise ValueError(f"{path} must contain multiple frames and a looping extension")


def validate_output_dir(output_dir: Path) -> None:
    missing = [name for name in EXPECTED if not (output_dir / name).is_file()]
    if missing:
        raise ValueError("missing artifacts: " + ", ".join(missing))
    light_view_box, light_geometry = validate_svg(output_dir / EXPECTED[0])
    dark_view_box, dark_geometry = validate_svg(output_dir / EXPECTED[1])
    if light_view_box != dark_view_box or light_geometry != dark_geometry:
        raise ValueError("light and dark SVGs do not have matching geometry")
    validate_gif(output_dir / EXPECTED[2])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    validate_output_dir(args.output_dir)
    print(f"Validated artifacts in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
