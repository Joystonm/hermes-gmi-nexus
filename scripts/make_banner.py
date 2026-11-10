"""Draw the catalog card banner (``docs/banner.png``).

The catalog entry's ``image:`` key wants a 2:1 picture (1200x600) on a GitHub
host, pinned to the entry's commit. This script draws it.

It is a **development** tool: it is not part of the plugin payload, the plugin
imports nothing but the standard library, and Pillow is needed only to run this
once. The PNG it writes is committed; the script never runs on a user's
machine.

    python scripts/make_banner.py

The palette is the dashboard's own — the ink, the blue, the sky and the teal
from ``dashboard/dist/style.css`` — so the card, the wordmark and the plugin
tab read as one thing.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

#: The catalogue card's 2:1 geometry.
WIDTH, HEIGHT = 1200, 600

#: The dashboard's palette.
INK = (11, 15, 25)
INK_DEEP = (7, 10, 18)
BLUE = (37, 99, 235)
SKY = (14, 165, 233)
TEAL = (20, 184, 166)
WHITE = (255, 255, 255)
MUTED = (154, 167, 189)
FAINT = (91, 100, 120)

#: Where the one beam becomes many.
SPLIT = (690, 300)
OUTGOING = 7
FAN = [(1155, 74), (1155, 158), (1155, 232), (1155, 300), (1155, 368), (1155, 442), (1155, 526)]

FONT_DIR = Path("C:/Windows/Fonts")
BOLD = FONT_DIR / "segoeuib.ttf"
REGULAR = FONT_DIR / "segoeui.ttf"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    """Load a system font, falling back to Pillow's own if it is missing."""
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:  # pragma: no cover - depends on the machine
        return ImageFont.load_default()


def gradient() -> Image.Image:
    """A diagonal wash from the deep ink to a slightly lifted navy."""
    base = Image.new("RGB", (WIDTH, HEIGHT), INK_DEEP)
    draw = ImageDraw.Draw(base)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        row = tuple(
            int(INK_DEEP[i] + (INK[i] - INK_DEEP[i]) * (0.25 + 0.75 * t)) for i in range(3)
        )
        draw.line([(0, y), (WIDTH, y)], fill=row)
    return base


def dot_grid(image: Image.Image) -> None:
    """A faint developer grid, brightest behind the beams."""
    layer = Image.new("RGBA", (WIDTH * 2, HEIGHT * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for gy in range(0, HEIGHT * 2, 56):
        for gx in range(0, WIDTH * 2, 56):
            dx = (gx - SPLIT[0] * 2) / (WIDTH * 1.1)
            dy = (gy - SPLIT[1] * 2) / (HEIGHT * 1.1)
            fall = max(0.0, 1.0 - math.hypot(dx, dy))
            alpha = int(10 + 26 * fall)
            draw.ellipse([gx - 1, gy - 1, gx + 1, gy + 1], fill=(150, 175, 215, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(0.6))
    image.alpha_composite(layer.resize((WIDTH, HEIGHT), Image.LANCZOS))


def beams(image: Image.Image) -> None:
    """One beam in, many out — the product's own idea, drawn."""
    scale = 2
    glow = Image.new("RGBA", (WIDTH * scale, HEIGHT * scale), (0, 0, 0, 0))
    core = Image.new("RGBA", (WIDTH * scale, HEIGHT * scale), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cdraw = ImageDraw.Draw(core)

    def p(point: tuple[int, int]) -> tuple[int, int]:
        return (point[0] * scale, point[1] * scale)

    # The single beam arriving from the left. It starts clear of the wordmark.
    arrival = ((582, 300), SPLIT)
    gdraw.line([p(arrival[0]), p(arrival[1])], fill=BLUE + (130,), width=30 * scale)
    cdraw.line([p(arrival[0]), p(arrival[1])], fill=BLUE + (245,), width=4 * scale)

    # And the many it becomes.
    for index, end in enumerate(FAN):
        t = index / (len(FAN) - 1)
        colour = tuple(
            int(BLUE[i] + (TEAL[i] - BLUE[i]) * t) for i in range(3)
        )
        gdraw.line([p(SPLIT), p(end)], fill=colour + (120,), width=22 * scale)
        cdraw.line([p(SPLIT), p(end)], fill=colour + (240,), width=3 * scale)

    # The split itself: a small bright point where the one becomes many.
    for radius, alpha in ((58, 20), (34, 46), (14, 120)):
        box = [SPLIT[0] * scale - radius * scale, SPLIT[1] * scale - radius * scale,
               SPLIT[0] * scale + radius * scale, SPLIT[1] * scale + radius * scale]
        gdraw.ellipse(box, fill=SKY + (alpha,))

    glow = glow.filter(ImageFilter.GaussianBlur(16 * scale / 2))
    image.alpha_composite(glow.resize((WIDTH, HEIGHT), Image.LANCZOS))
    image.alpha_composite(core.resize((WIDTH, HEIGHT), Image.LANCZOS))


def tracked(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, face, fill, tracking: int) -> int:
    """Draw text with letter-spacing, the way a wordmark is set."""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=face, fill=fill)
        x += int(draw.textlength(char, font=face)) + tracking
    return x


def wordmark(image: Image.Image) -> None:
    """The name, the rule under it, and the line that says what it is."""
    draw = ImageDraw.Draw(image)
    tracked(draw, (86, 214), "GMI NEXUS", font(BOLD, 74), WHITE + (255,), 6)
    draw.rectangle([86, 322, 86 + 108, 322 + 5], fill=BLUE + (255,))
    draw.rectangle([86 + 116, 322, 86 + 116 + 108, 322 + 5], fill=TEAL + (255,))
    tracked(draw, (86, 348), "One agent. Many models. One intelligent system.",
            font(REGULAR, 25), MUTED + (255,), 0)
    tracked(draw, (86, 505), "Hermes Agent plugin  ·  GMI Cloud",
            font(REGULAR, 19), FAINT + (255,), 1)


def build() -> Image.Image:
    image = gradient().convert("RGBA")
    dot_grid(image)
    beams(image)
    wordmark(image)
    return image.convert("RGB")


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "docs" / "banner.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    build().save(out, format="PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    main()
