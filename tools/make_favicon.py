"""Generates favicon.ico — a 'C' monogram badge (Ceaden) in the app's own
dark/amber palette, rendered at high resolution then downsampled into a
multi-size .ico (Windows uses the size that best matches each context:
16/32 for window/taskbar icons, 256 for the installer and Explorer)."""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_ICO = os.path.join(ROOT, "favicon.ico")
OUT_PNG_PREVIEW = os.path.join(HERE, "favicon_preview.png")

BG = (16, 21, 28, 255)        # #10151c
RING = (255, 180, 84, 255)    # #ffb454 (accent)
LETTER = (255, 180, 84, 255)  # #ffb454 (accent)

SIZE = 1024  # render large, downsample for crisp small sizes
SIZES = [16, 24, 32, 48, 64, 128, 256]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",   # Segoe UI Bold
    r"C:\Windows\Fonts\arialbd.ttf",    # Arial Bold
    r"C:\Windows\Fonts\calibrib.ttf",
]


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_base_image():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = int(SIZE * 0.04)
    draw.ellipse([margin, margin, SIZE - margin, SIZE - margin], fill=BG)

    ring_w = int(SIZE * 0.035)
    draw.ellipse(
        [margin + ring_w // 2, margin + ring_w // 2, SIZE - margin - ring_w // 2, SIZE - margin - ring_w // 2],
        outline=RING, width=ring_w,
    )

    # Letter "C" as an open arc rather than the literal glyph, so it reads
    # as a mark/badge rather than plain text at small sizes.
    letter_margin = int(SIZE * 0.26)
    bbox = [letter_margin, letter_margin, SIZE - letter_margin, SIZE - letter_margin]
    arc_w = int(SIZE * 0.14)
    draw.arc(bbox, start=35, end=325, fill=LETTER, width=arc_w)
    # Round the arc's two open ends (PIL's arc has flat caps).
    r = arc_w / 2
    cx, cy = SIZE / 2, SIZE / 2
    rad = (SIZE - 2 * letter_margin) / 2
    import math
    for deg in (35, 325):
        rad_a = math.radians(deg)
        x = cx + rad * math.cos(rad_a)
        y = cy + rad * math.sin(rad_a)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=LETTER)

    return img


def main():
    base = make_base_image()
    base.save(OUT_PNG_PREVIEW)

    imgs = []
    for s in SIZES:
        imgs.append(base.resize((s, s), Image.LANCZOS))
    imgs[0].save(OUT_ICO, format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[1:])
    print(f"Wrote {OUT_ICO} with sizes {SIZES}")
    print(f"Preview PNG: {OUT_PNG_PREVIEW}")


if __name__ == "__main__":
    main()
