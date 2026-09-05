"""Debug tool: dump per-span text/color/bbox/font/flags to calibrate the question marker color."""

from __future__ import annotations

import sys
from collections import Counter

import fitz


def rgb(color_int: int) -> tuple[int, int, int]:
    return ((color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255)


def main(path: str) -> None:
    doc = fitz.open(path)
    colors: Counter = Counter()
    for pno, page in enumerate(doc, start=1):
        print(f"\n===== PAGE {pno} =====")
        data = page.get_text("dict")
        for block in data["blocks"]:
            if block["type"] != 0:
                print(f"  [IMAGE BLOCK] bbox={tuple(round(v,1) for v in block['bbox'])}")
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"]
                    if not text.strip():
                        continue
                    c = rgb(span["color"])
                    colors[c] += len(text)
                    print(
                        f"  rgb={c} flags={span['flags']:>4} size={span['size']:.1f} "
                        f"font={span['font'][:28]:<28} "
                        f"bbox=({span['bbox'][0]:.0f},{span['bbox'][1]:.0f},{span['bbox'][2]:.0f},{span['bbox'][3]:.0f}) "
                        f"| {text[:70]!r}"
                    )
        for img in page.get_images(full=True):
            try:
                bbox = page.get_image_bbox(img)
            except Exception as exc:
                bbox = f"<err {exc}>"
            print(f"  [IMAGE XObject] xref={img[0]} bbox={bbox}")

    print("\n===== COLOR HISTOGRAM (chars per rgb) =====")
    for c, n in colors.most_common(40):
        print(f"  {c}: {n}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pdf/1.praktikum hematopoees.pdf")
