from __future__ import annotations

import re

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC = re.compile(
    r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE
)


def img_tag(filename: str) -> str:
    """The canonical inline form; parsing emits this and cleanup must return it verbatim."""
    return f'<img src="{filename}">'


def img_src(tag: str) -> str:
    match = _SRC.search(tag)
    if match is None:
        return ""
    return next(group for group in match.groups() if group is not None)


def referenced_images(html: str) -> list[str]:
    """Filenames referenced by <img> in document order, without duplicates."""
    names = (img_src(tag) for tag in _IMG_TAG.findall(html))
    return list(dict.fromkeys(name for name in names if name))


def strip_images(html: str, filenames: set[str] | list[str]) -> str:
    drop = set(filenames)
    return _IMG_TAG.sub(lambda m: "" if img_src(m.group(0)) in drop else m.group(0), html)


def dedupe_images(html: str, seen: set[str]) -> str:
    """Drop <img> tags whose src is already in `seen`, which grows as new ones appear.

    The first occurrence is the one kept: its position is the meaningful one.
    """

    def keep(match: re.Match[str]) -> str:
        src = img_src(match.group(0))
        if src in seen:
            return ""
        seen.add(src)
        return match.group(0)

    return _IMG_TAG.sub(keep, html)
