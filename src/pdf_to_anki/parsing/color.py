from __future__ import annotations

DEFAULT_MARKER_RGB = (80, 148, 110)
DEFAULT_TOLERANCE = 30

RGB = tuple[int, int, int]


def rgb_from_span_color(color_int: int) -> RGB:
    return ((color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255)


def is_question_color(
    rgb: RGB,
    target: RGB = DEFAULT_MARKER_RGB,
    tolerance: int = DEFAULT_TOLERANCE,
) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(rgb, target))
