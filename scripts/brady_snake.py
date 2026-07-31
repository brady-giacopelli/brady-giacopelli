#!/usr/bin/env python3
"""Build and validate Brady's deterministic contribution-grid snake.

The renderer deliberately consumes the normalized grid model in this file,
instead of passing a username to Platane/snk.  This keeps the 52-week window,
blank border columns, and BRADY bitmap stable while retaining a snake animation
over the active canvas.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from xml.sax.saxutils import escape


ACTIVE_COLUMNS = 52
ROWS = 7
RENDERED_COLUMNS = ACTIVE_COLUMNS + 2
SEPARATOR_WIDTH = 1
RENDERER_VERSION = "brady-snake-renderer-v1"
PATTERN_PATH = Path(__file__).resolve().parents[1] / "assets" / "brady-pattern-v1.json"

CELL_SIZE = 12
CELL_GAP = 3
PADDING = 12
SNAKE_LENGTH = 10
GIF_FRAME_COUNT = 104
GIF_DELAY_CENTISECONDS = 7

LIGHT_COLORS = {
    "background": "#ffffff",
    "empty": "#ebedf0",
    "active": "#216e39",
    "snake": "#0969da",
    "head": "#cf222e",
}
DARK_COLORS = {
    "background": "#0d1117",
    "empty": "#21262d",
    "active": "#39d353",
    "snake": "#58a6ff",
    "head": "#ff7b72",
}


@dataclass(frozen=True)
class Pattern:
    version: str
    word: str
    glyph_width: int
    glyph_height: int
    separator_width: int
    glyphs: dict[str, tuple[str, ...]]

    @property
    def word_width(self) -> int:
        return len(self.word) * self.glyph_width + (len(self.word) - 1) * self.separator_width


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start.weekday() != 6:
            raise ValueError("date-window start must be a Sunday")
        if self.end != self.start + timedelta(days=ACTIVE_COLUMNS * ROWS - 1):
            raise ValueError("date-window must contain exactly 52 complete weeks")
        if self.end.weekday() != 5:
            raise ValueError("date-window end must be a Saturday")


@dataclass(frozen=True)
class GridCell:
    column: int
    row: int
    cell_date: date
    active: bool
    future: bool


@dataclass(frozen=True)
class GridModel:
    cutoff: date
    window: DateWindow
    pattern: Pattern
    cells: tuple[tuple[GridCell, ...], ...]

    @property
    def active_cell_count(self) -> int:
        return sum(cell.active for row in self.cells for cell in row)

    @property
    def rendered_values(self) -> tuple[tuple[int, ...], ...]:
        """Return the 54-column canvas, including permanently blank borders."""

        return tuple(
            (0,) + tuple(int(cell.active) for cell in row) + (0,)
            for row in self.cells
        )


def load_pattern(path: Path = PATTERN_PATH) -> Pattern:
    raw = json.loads(path.read_text(encoding="utf-8"))
    pattern = Pattern(
        version=raw["version"],
        word=raw["word"],
        glyph_width=raw["glyph_width"],
        glyph_height=raw["glyph_height"],
        separator_width=raw["separator_width"],
        glyphs={key: tuple(value) for key, value in raw["glyphs"].items()},
    )
    validate_pattern(pattern)
    return pattern


def validate_pattern(pattern: Pattern) -> None:
    if pattern.glyph_height != ROWS:
        raise ValueError("BRADY glyphs must be seven rows high")
    if not pattern.word or any(letter not in pattern.glyphs for letter in pattern.word):
        raise ValueError("pattern word contains a missing glyph")
    for letter, glyph in pattern.glyphs.items():
        if len(glyph) != pattern.glyph_height:
            raise ValueError(f"glyph {letter} must have seven rows")
        if any(len(row) != pattern.glyph_width or set(row) - {"0", "1"} for row in glyph):
            raise ValueError(f"glyph {letter} must be a binary 5x7 bitmap")
    if pattern.word_width != 29:
        raise ValueError("BRADY must occupy exactly 29 columns")
    if pattern.word_width > ACTIVE_COLUMNS:
        raise ValueError("BRADY bitmap does not fit in the active grid")


def brady_bitmap(pattern: Pattern) -> tuple[tuple[int, ...], ...]:
    """Compose the versioned glyphs and center the 29-column word."""

    left_padding = (ACTIVE_COLUMNS - pattern.word_width) // 2
    right_padding = ACTIVE_COLUMNS - pattern.word_width - left_padding
    rows = [[0] * left_padding for _ in range(ROWS)]
    for row_index in range(ROWS):
        for letter_index, letter in enumerate(pattern.word):
            rows[row_index].extend(int(bit) for bit in pattern.glyphs[letter][row_index])
            if letter_index != len(pattern.word) - 1:
                rows[row_index].extend([0] * pattern.separator_width)
        rows[row_index].extend([0] * right_padding)
    bitmap = tuple(tuple(row) for row in rows)
    if any(len(row) != ACTIVE_COLUMNS for row in bitmap):
        raise AssertionError("composed BRADY bitmap is not 52 columns wide")
    return bitmap


def _as_utc_date(value: date | datetime | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime cutoffs must include a timezone")
        return value.astimezone(timezone.utc).date()
    return value


def window_for_utc(value: date | datetime | None = None) -> DateWindow:
    """Return the last 52 complete Sunday-first weeks before the current week."""

    cutoff = _as_utc_date(value)
    days_since_sunday = (cutoff.weekday() + 1) % 7
    current_week_start = cutoff - timedelta(days=days_since_sunday)
    end = current_week_start - timedelta(days=1)
    start = end - timedelta(days=ACTIVE_COLUMNS * ROWS - 1)
    return DateWindow(start=start, end=end)


def build_grid(
    value: date | datetime | None = None,
    *,
    window: DateWindow | None = None,
    pattern: Pattern | None = None,
) -> GridModel:
    cutoff = _as_utc_date(value)
    selected_window = window or window_for_utc(cutoff)
    selected_pattern = pattern or load_pattern()
    bitmap = brady_bitmap(selected_pattern)
    cells: list[tuple[GridCell, ...]] = []
    for row_index in range(ROWS):
        row: list[GridCell] = []
        for column_index in range(ACTIVE_COLUMNS):
            cell_date = selected_window.start + timedelta(days=column_index * ROWS + row_index)
            is_future = cell_date > cutoff
            row.append(
                GridCell(
                    column=column_index,
                    row=row_index,
                    cell_date=cell_date,
                    active=bool(bitmap[row_index][column_index] and not is_future),
                    future=is_future,
                )
            )
        cells.append(tuple(row))
    model = GridModel(
        cutoff=cutoff,
        window=selected_window,
        pattern=selected_pattern,
        cells=tuple(cells),
    )
    validate_grid(model)
    return model


def validate_grid(model: GridModel) -> None:
    if len(model.cells) != ROWS or any(len(row) != ACTIVE_COLUMNS for row in model.cells):
        raise ValueError("grid must contain exactly 52 columns and seven rows")
    bitmap = brady_bitmap(model.pattern)
    for row_index, row in enumerate(model.cells):
        for column_index, cell in enumerate(row):
            expected_date = model.window.start + timedelta(days=column_index * ROWS + row_index)
            if (cell.column, cell.row, cell.cell_date) != (column_index, row_index, expected_date):
                raise ValueError("grid cell date mapping is not Sunday-first")
            if cell.future and cell.active:
                raise ValueError("future cells must be empty")
            if not cell.future and cell.active != bool(bitmap[row_index][column_index]):
                raise ValueError("active cell does not match the BRADY bitmap")


def canvas_size() -> tuple[int, int]:
    width = PADDING * 2 + RENDERED_COLUMNS * CELL_SIZE + (RENDERED_COLUMNS - 1) * CELL_GAP
    height = PADDING * 2 + ROWS * CELL_SIZE + (ROWS - 1) * CELL_GAP
    return width, height


def cell_position(column: int, row: int) -> tuple[int, int]:
    return PADDING + column * (CELL_SIZE + CELL_GAP), PADDING + row * (CELL_SIZE + CELL_GAP)


def snake_path() -> tuple[tuple[int, int], ...]:
    """Return a contiguous serpentine path over the 52 active columns."""

    path: list[tuple[int, int]] = []
    for column in range(1, ACTIVE_COLUMNS + 1):
        row_range = range(ROWS) if column % 2 else range(ROWS - 1, -1, -1)
        path.extend((column, row) for row in row_range)
    return tuple(path)


def _svg_animation(values: Sequence[str], duration: str = "12s") -> str:
    key_times = ";".join(f"{index / (len(values) - 1):.6f}" for index in range(len(values)))
    joined = ";".join(values)
    return (
        f'<animate attributeName="x" values="{joined}" keyTimes="{key_times}" '
        f'dur="{duration}" calcMode="discrete" repeatCount="indefinite" />'
    )


def _svg_y_animation(values: Sequence[str], duration: str = "12s") -> str:
    key_times = ";".join(f"{index / (len(values) - 1):.6f}" for index in range(len(values)))
    joined = ";".join(values)
    return (
        f'<animate attributeName="y" values="{joined}" keyTimes="{key_times}" '
        f'dur="{duration}" calcMode="discrete" repeatCount="indefinite" />'
    )


def render_svg(model: GridModel, colors: dict[str, str]) -> str:
    width, height = canvas_size()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'data-grid-columns="{RENDERED_COLUMNS}" data-grid-rows="{ROWS}" '
        f'data-active-columns="{ACTIVE_COLUMNS}" data-pattern-version="{escape(model.pattern.version)}" '
        f'data-renderer-version="{RENDERER_VERSION}" '
        f'data-window-start="{model.window.start.isoformat()}" data-window-end="{model.window.end.isoformat()}" '
        f'data-active-cell-count="{model.active_cell_count}">',
        '<title>BRADY contribution grid snake</title>',
        f'<desc>52 complete UTC weeks from {model.window.start.isoformat()} through '
        f'{model.window.end.isoformat()}, rendered with blank border columns.</desc>',
        f'<rect width="{width}" height="{height}" fill="{colors["background"]}" />',
        '<g id="grid" shape-rendering="crispEdges">',
    ]
    for row_index, row in enumerate(model.cells):
        rendered_row = (None,) + row + (None,)
        for rendered_column, cell in enumerate(rendered_row):
            x, y = cell_position(rendered_column, row_index)
            border = rendered_column in (0, RENDERED_COLUMNS - 1)
            active = bool(cell and cell.active and not border)
            cell_date = cell.cell_date.isoformat() if cell else ""
            lines.append(
                f'<rect x="{x}" y="{y}" width="{CELL_SIZE}" height="{CELL_SIZE}" '
                f'fill="{colors["active"] if active else colors["empty"]}" '
                f'data-col="{rendered_column}" data-row="{row_index}" data-date="{cell_date}" '
                f'data-active="{int(active)}" data-border="{str(border).lower()}" />'
            )
    lines.extend(["</g>", '<g id="snake" shape-rendering="crispEdges">'])
    path = snake_path()
    for segment in range(SNAKE_LENGTH):
        positions = [path[(index - segment) % len(path)] for index in range(len(path) + 1)]
        x_values = [str(cell_position(column, row)[0]) for column, row in positions]
        y_values = [str(cell_position(column, row)[1]) for column, row in positions]
        color = colors["head"] if segment == 0 else colors["snake"]
        lines.append(
            f'<rect x="{x_values[0]}" y="{y_values[0]}" width="{CELL_SIZE}" height="{CELL_SIZE}" '
            f'fill="{color}" data-snake-segment="{segment}">'
            f'{_svg_animation(x_values)}{_svg_y_animation(y_values)}</rect>'
        )
    lines.extend(["</g>", "</svg>"])
    return "\n".join(lines) + "\n"


GIF_PALETTE = (
    (255, 255, 255),
    (235, 237, 240),
    (33, 110, 57),
    (9, 105, 218),
    (207, 34, 46),
    (13, 17, 23),
    (33, 38, 45),
    (57, 211, 83),
    (88, 166, 255),
    (255, 123, 114),
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
)


def _lzw_encode(indexes: Sequence[int], minimum_code_size: int = 3) -> bytes:
    """Encode one GIF image with a small, self-contained LZW encoder."""

    clear_code = 1 << minimum_code_size
    end_code = clear_code + 1
    code_size = minimum_code_size + 1
    next_code = end_code + 1
    dictionary = {(index,): index for index in range(clear_code)}
    bits: list[int] = []

    def emit(code: int, width: int) -> None:
        bits.extend((code >> bit) & 1 for bit in range(width))

    emit(clear_code, code_size)
    if indexes:
        current = (indexes[0],)
        for index in indexes[1:]:
            candidate = current + (index,)
            if candidate in dictionary:
                current = candidate
                continue
            emit(dictionary[current], code_size)
            if next_code < 4096:
                dictionary[candidate] = next_code
                next_code += 1
                # The decoder adds a dictionary entry one code later than the
                # encoder, so widen the emitted code one slot after the power
                # of two boundary.
                if next_code == (1 << code_size) + 1 and code_size < 12:
                    code_size += 1
            else:
                emit(clear_code, code_size)
                dictionary = {(value,): value for value in range(clear_code)}
                code_size = minimum_code_size + 1
                next_code = end_code + 1
            current = (index,)
        emit(dictionary[current], code_size)
    emit(end_code, code_size)
    output = bytearray()
    for offset in range(0, len(bits), 8):
        value = 0
        for bit_index, bit in enumerate(bits[offset : offset + 8]):
            value |= bit << bit_index
        output.append(value)
    return bytes(output)


def _lzw_decode(data: bytes, minimum_code_size: int) -> list[int]:
    """Decode a GIF image stream for validation without an external library."""

    clear_code = 1 << minimum_code_size
    end_code = clear_code + 1
    code_size = minimum_code_size + 1
    next_code = end_code + 1
    dictionary = {index: [index] for index in range(clear_code)}
    bits = [(value >> bit) & 1 for value in data for bit in range(8)]
    position = 0
    previous: list[int] | None = None
    decoded: list[int] = []
    while position + code_size <= len(bits):
        code = sum(bits[position + bit] << bit for bit in range(code_size))
        position += code_size
        if code == clear_code:
            code_size = minimum_code_size + 1
            next_code = end_code + 1
            dictionary = {index: [index] for index in range(clear_code)}
            previous = None
            continue
        if code == end_code:
            return decoded
        if code in dictionary:
            entry = dictionary[code]
        elif code == next_code and previous is not None:
            entry = previous + [previous[0]]
        else:
            raise ValueError("GIF LZW stream contains an invalid code")
        decoded.extend(entry)
        if previous is not None:
            dictionary[next_code] = previous + [entry[0]]
            next_code += 1
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1
        previous = entry
    raise ValueError("GIF LZW stream is missing its end code")


def _gif_frame(model: GridModel, path_index: int, colors: dict[str, str]) -> list[int]:
    width, height = canvas_size()
    pixels = [0] * (width * height)
    if colors is DARK_COLORS:
        background, empty, active, snake, head = 5, 6, 7, 8, 9
    else:
        background, empty, active, snake, head = 0, 1, 2, 3, 4

    def paint_cell(column: int, row: int, color_index: int) -> None:
        x, y = cell_position(column, row)
        for yy in range(y, y + CELL_SIZE):
            start = yy * width + x
            pixels[start : start + CELL_SIZE] = [color_index] * CELL_SIZE

    # Background and seven-row grid. The two border columns intentionally use
    # the same empty color as all other inactive cells.
    for yy in range(height):
        pixels[yy * width : (yy + 1) * width] = [background] * width
    rendered = model.rendered_values
    for row_index, row in enumerate(rendered):
        for column_index, value in enumerate(row):
            paint_cell(column_index, row_index, active if value else empty)

    path = snake_path()
    for segment in range(SNAKE_LENGTH - 1, -1, -1):
        column, row = path[(path_index - segment) % len(path)]
        paint_cell(column, row, head if segment == 0 else snake)
    return pixels


def _hex_to_gif_palette(colors: dict[str, str]) -> tuple[tuple[int, int, int], ...]:
    """Return the fixed GIF palette; themes are selected in _gif_frame."""

    return GIF_PALETTE


def render_gif(model: GridModel, colors: dict[str, str]) -> bytes:
    width, height = canvas_size()
    frames = [
        _gif_frame(
            model,
            min(len(snake_path()) - 1, int(index * len(snake_path()) / GIF_FRAME_COUNT)),
            colors,
        )
        for index in range(GIF_FRAME_COUNT)
    ]
    output = bytearray(b"GIF89a")
    output.extend(struct.pack("<HH", width, height))
    # 16-entry global color table: 0x80 flag + 0x70 color resolution + 0x03 size.
    output.extend(bytes((0xF3, 0, 0)))
    for red, green, blue in _hex_to_gif_palette(colors):
        output.extend(bytes((red, green, blue)))
    # Netscape loop extension with a zero loop count means repeat forever.
    output.extend(b"!\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")
    for frame in frames:
        output.extend(b"!\xF9\x04\x00")
        output.extend(struct.pack("<H", GIF_DELAY_CENTISECONDS))
        output.extend(b"\x00\x00")
        output.extend(b",\x00\x00\x00\x00")
        output.extend(struct.pack("<HH", width, height))
        output.extend(b"\x00")
        compressed = _lzw_encode(frame, minimum_code_size=4)
        output.append(4)
        for offset in range(0, len(compressed), 255):
            block = compressed[offset : offset + 255]
            output.append(len(block))
            output.extend(block)
        output.append(0)
    output.append(0x3B)
    return bytes(output)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _grid_rects(root: ET.Element) -> list[ET.Element]:
    for element in root.iter():
        if _local_name(element.tag) == "g" and element.attrib.get("id") == "grid":
            return [child for child in element if _local_name(child.tag) == "rect"]
    raise ValueError("SVG is missing its logical grid group")


def validate_svg(path: Path, expected_active_count: int) -> tuple[tuple[str, ...], ...]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise ValueError(f"{path} is not a parseable SVG: {error}") from error
    if _local_name(root.tag) != "svg":
        raise ValueError(f"{path} does not contain an SVG root")
    if root.attrib.get("data-grid-columns") != str(RENDERED_COLUMNS) or root.attrib.get("data-grid-rows") != str(ROWS):
        raise ValueError(f"{path} has the wrong grid dimensions")
    rects = _grid_rects(root)
    if len(rects) != RENDERED_COLUMNS * ROWS:
        raise ValueError(f"{path} must contain exactly 54x7 logical cells")
    rows = {int(rect.attrib["data-row"]) for rect in rects}
    row_counts = {row: sum(int(rect.attrib["data-row"]) == row for rect in rects) for row in rows}
    if rows != set(range(ROWS)) or any(count != RENDERED_COLUMNS for count in row_counts.values()):
        raise ValueError(f"{path} must contain exactly seven logical rows")
    if root.attrib.get("data-renderer-version") != RENDERER_VERSION:
        raise ValueError(f"{path} was not produced by {RENDERER_VERSION}")
    geometry: list[tuple[str, ...]] = []
    active_count = 0
    for rect in rects:
        column = int(rect.attrib["data-col"])
        row = int(rect.attrib["data-row"])
        if not 0 <= column < RENDERED_COLUMNS or not 0 <= row < ROWS:
            raise ValueError(f"{path} contains a cell outside the rendered canvas")
        active = rect.attrib.get("data-active") == "1"
        if active:
            active_count += 1
        if column in (0, RENDERED_COLUMNS - 1) and (active or rect.attrib.get("data-border") != "true"):
            raise ValueError(f"{path} has a non-blank border cell")
        geometry.append(
            (
                str(row),
                str(column),
                rect.attrib.get("x", ""),
                rect.attrib.get("y", ""),
                rect.attrib.get("width", ""),
                rect.attrib.get("height", ""),
                rect.attrib.get("data-date", ""),
                rect.attrib.get("data-active", ""),
                rect.attrib.get("data-border", ""),
            )
        )
    if active_count != expected_active_count:
        raise ValueError(f"{path} has {active_count} active cells, expected {expected_active_count}")
    return tuple(sorted(geometry))


def validate_gif(path: Path) -> None:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not read {path}: {error}") from error
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a") or data[-1] != 0x3B:
        raise ValueError(f"{path} is not a complete GIF")
    width, height = struct.unpack_from("<HH", data, 6)
    if width == 0 or height == 0:
        raise ValueError(f"{path} has an empty canvas")
    packed = data[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    image_count = 0
    first_frame_pixels: list[int] | None = None
    has_loop_extension = b"NETSCAPE2.0" in data
    while offset < len(data) - 1:
        marker = data[offset]
        offset += 1
        if marker == 0x21:
            if offset >= len(data):
                raise ValueError(f"{path} has a truncated extension")
            offset += 1
            while True:
                if offset >= len(data):
                    raise ValueError(f"{path} has a truncated extension block")
                size = data[offset]
                offset += 1
                if size == 0:
                    break
                offset += size
        elif marker == 0x2C:
            if offset + 9 > len(data):
                raise ValueError(f"{path} has a truncated image descriptor")
            offset += 9
            image_packed = data[offset - 1]
            if image_packed & 0x80:
                offset += 3 * (2 ** ((image_packed & 0x07) + 1))
            if offset >= len(data):
                raise ValueError(f"{path} has no image data")
            minimum_code_size = data[offset]
            offset += 1
            image_data = bytearray()
            block_bytes = 0
            while True:
                if offset >= len(data):
                    raise ValueError(f"{path} has truncated image data")
                size = data[offset]
                offset += 1
                if size == 0:
                    break
                block_bytes += size
                image_data.extend(data[offset : offset + size])
                offset += size
            if block_bytes == 0:
                raise ValueError(f"{path} has an empty image frame")
            if first_frame_pixels is None:
                first_frame_pixels = _lzw_decode(bytes(image_data), minimum_code_size)
            image_count += 1
        elif marker == 0x3B:
            break
        else:
            raise ValueError(f"{path} contains an unknown GIF block 0x{marker:02x}")
    if image_count < 2 or not has_loop_extension:
        raise ValueError(f"{path} must contain multiple frames and a looping extension")
    if first_frame_pixels is None or len(first_frame_pixels) != width * height:
        raise ValueError(f"{path} does not decode to its declared canvas size")


def validate_output_dir(output_dir: Path) -> None:
    expected = load_pattern()
    expected_active_count = sum(
        int(bit)
        for row in brady_bitmap(expected)
        for bit in row
    )
    light = output_dir / "github-contribution-grid-snake.svg"
    dark = output_dir / "github-contribution-grid-snake-dark.svg"
    gif = output_dir / "github-contribution-grid-snake.gif"
    for path in (light, dark, gif):
        if not path.is_file():
            raise ValueError(f"missing required artifact: {path}")
    light_geometry = validate_svg(light, expected_active_count)
    dark_geometry = validate_svg(dark, expected_active_count)
    if light_geometry != dark_geometry:
        raise ValueError("light and dark SVGs do not have matching geometry")
    validate_gif(gif)


def _summary(model: GridModel) -> str:
    return (
        "## BRADY snake\n"
        f"- UTC grid window: `{model.window.start.isoformat()}` → `{model.window.end.isoformat()}`\n"
        f"- Pattern: `{model.pattern.version}` (`{model.pattern.word}`, {model.active_cell_count} active cells)\n"
        f"- Canvas: `{RENDERED_COLUMNS}×{ROWS}` (`{ACTIVE_COLUMNS}` active columns + two blank border columns)\n"
    )


def generate(output_dir: Path, value: date | datetime | None = None, summary_file: Path | None = None) -> GridModel:
    model = build_grid(value)
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=".brady-snake-", dir=output_dir.parent))
    try:
        (temporary_dir / "github-contribution-grid-snake.svg").write_text(
            render_svg(model, LIGHT_COLORS), encoding="utf-8"
        )
        (temporary_dir / "github-contribution-grid-snake-dark.svg").write_text(
            render_svg(model, DARK_COLORS), encoding="utf-8"
        )
        (temporary_dir / "github-contribution-grid-snake.gif").write_bytes(render_gif(model, LIGHT_COLORS))
        validate_output_dir(temporary_dir)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        temporary_dir.rename(output_dir)
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    if summary_file:
        with summary_file.open("a", encoding="utf-8") as handle:
            handle.write(_summary(model))
    return model


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--as-of", type=_parse_date, help="UTC date for deterministic local generation")
    parser.add_argument("--summary-file", type=Path, help="Append the window/pattern summary to this file")
    parser.add_argument("--validate", type=Path, metavar="DIR", help="Validate an existing output directory")
    args = parser.parse_args()
    if args.validate:
        validate_output_dir(args.validate)
        print(f"Validated artifacts in {args.validate}")
        return 0
    model = generate(args.output_dir, args.as_of, args.summary_file)
    print(_summary(model), end="")
    print(f"Generated validated artifacts in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
