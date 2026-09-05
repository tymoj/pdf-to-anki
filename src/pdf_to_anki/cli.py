from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError
from .pipeline import pdf_to_anki


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf-to-anki",
        description="Convert a colour-marked PDF into an Anki .apkg deck.",
    )
    parser.add_argument("input", type=Path, help="source PDF")
    parser.add_argument("-o", "--output", type=Path, default=None, help="output .apkg path")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        out = pdf_to_anki(args.input, args.output)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
