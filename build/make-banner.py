#!/usr/bin/env python3
"""生成 README 顶部的「字符雨」横幅，深浅两版。

用法：
    python3 build/make-banner.py

输出：
    assets/banner-light.png   浅色主题用（白底、深蓝字）
    assets/banner-dark.png    深色主题用（深蓝黑底、亮蓝字）

README 里用 <picture> + prefers-color-scheme 切换，GitHub 会按用户主题挑一张。
配色取自 build/icon-source.png：主蓝 #227afc、白、银灰。
字符雨斜向下落，标题带两端斜切，两版用同一个随机种子，形状完全对齐。
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1200, 300
COLUMNS = 46
CHAR_STEP = 16
TITLE = "CollabNote"
SUBTITLE = "MULTI-USER NOTES  ·  ELECTRON + PYTHON"
TAGLINE = "share notes  ·  track money  ·  stay in sync"
SEED = 20261015

# 雨的斜率：每往下 100px 向右偏 18px，约 10°
RAIN_SLOPE = 0.18
# 标题带两端斜切的高度
BAND_SKEW = 30

GLYPHS = list("ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789<>[]{}=+*#$%&@")

FONT_DIR = Path("/System/Library/Fonts")
MENLO = FONT_DIR / "Menlo.ttc"

THEMES = {
    "dark": {
        "bg": ((5, 8, 15), (6, 12, 22), (2, 3, 6)),
        "rain": (61, 140, 255),
        "rain_head": (214, 232, 255),
        "title": ((247, 249, 255), (43, 124, 252)),
        "subtitle": (143, 188, 255),
        "tagline": (95, 143, 214),
        "band": (3, 7, 14, 182),
        "band_line": (77, 155, 255, 95),
        "glow": (77, 155, 255),
        "glow_strength": 0.38,
    },
    "light": {
        "bg": ((255, 255, 255), (238, 244, 251), (247, 249, 252)),
        "rain": (150, 180, 220),
        "rain_head": (43, 124, 252),
        "title": ((22, 58, 120), (11, 42, 99)),
        "subtitle": (60, 116, 200),
        "tagline": (110, 145, 190),
        "band": (247, 250, 255, 196),
        "band_line": (120, 170, 240, 130),
        "glow": (120, 170, 240),
        "glow_strength": 0.18,
    },
}


def load_rain_font(size):
    """字符雨要能显示半角片假名，Menlo 会画成豆腐块。"""
    for name in ("PingFang.ttc", "Hiragino Sans GB.ttc", "Menlo.ttc"):
        path = FONT_DIR / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def load_font(size, bold=False):
    for path, index in ((MENLO, 1 if bold else 0), (FONT_DIR / "Monaco.ttf", 0)):
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size, index=index)
            except OSError:
                continue
    return ImageFont.load_default()


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_background(theme):
    top, mid, bottom = theme["bg"]
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        color = lerp(top, mid, t / 0.55) if t < 0.55 else lerp(mid, bottom, (t - 0.55) / 0.45)
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return img


def draw_rain(img, theme, rng):
    layer = Image.new("RGBA", (WIDTH, HEIGHT + 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = load_rain_font(13)
    for col in range(COLUMNS):
        base_x = 10 + col * (WIDTH - 20) / (COLUMNS - 1)
        head = rng.randint(-8, 12)
        length = rng.randint(9, 19)
        for i in range(length):
            y = (head + i) * CHAR_STEP
            if y < -20 or y > HEIGHT + 40:
                continue
            x = base_x + y * RAIN_SLOPE
            if x < -20 or x > WIDTH + 20:
                continue
            fade = 1 - i / (length + 1)
            alpha = int(255 * (0.08 + 0.58 * fade ** 1.7))
            color = theme["rain_head"] if i == 0 else theme["rain"]
            draw.text((x, y), rng.choice(GLYPHS), font=font, fill=color + (alpha,))
    img.paste(layer.crop((0, 0, WIDTH, HEIGHT)), (0, 0), layer.crop((0, 0, WIDTH, HEIGHT)))


def draw_vignette(img, theme):
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    edge_rgb = theme["band"][:3]
    band = 90
    for i in range(band):
        alpha = int(120 * (1 - i / band))
        draw.line([(0, HEIGHT - 1 - i), (WIDTH, HEIGHT - 1 - i)], fill=edge_rgb + (alpha,))
        draw.line([(i, 0), (i, HEIGHT)], fill=edge_rgb + (int(alpha * 0.4),))
        draw.line([(WIDTH - 1 - i, 0), (WIDTH - 1 - i, HEIGHT)], fill=edge_rgb + (int(alpha * 0.4),))
    img.paste(overlay, (0, 0), overlay)


def draw_skewed_band(img, theme):
    """标题背后的斜切色带，左右两端各切一刀。"""
    top, bottom = 92, 242
    polygon = [
        (0, top + BAND_SKEW),
        (WIDTH, top),
        (WIDTH, bottom),
        (0, bottom - BAND_SKEW),
    ]
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.polygon(polygon, fill=theme["band"])
    line = theme["band_line"]
    draw.line([(0, top + BAND_SKEW), (WIDTH, top)], fill=line, width=2)
    draw.line([(0, bottom - BAND_SKEW), (WIDTH, bottom)], fill=line, width=2)
    img.paste(layer, (0, 0), layer)


def draw_spaced(draw, center_x, top_y, text, font, spacing, fill):
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = center_x - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, top_y), ch, font=font, fill=fill, anchor="lm")
        x += w + spacing
    return total


def add_title(img, theme, text, size, y, spacing):
    font = load_font(size, bold=True)
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    mdraw = ImageDraw.Draw(mask)
    draw_spaced(mdraw, WIDTH / 2, y, text, font, spacing, 255)

    top_color, bottom_color = theme["title"]
    gradient = Image.new("RGB", (WIDTH, HEIGHT))
    gdraw = ImageDraw.Draw(gradient)
    for row in range(HEIGHT):
        t = (row - y + size * 0.9) / (size * 1.1)
        gdraw.line([(0, row), (WIDTH, row)], fill=lerp(top_color, bottom_color, t))

    strength = theme["glow_strength"]
    if strength:
        halo = mask.filter(ImageFilter.GaussianBlur(9))
        halo_img = Image.new("RGB", (WIDTH, HEIGHT), theme["glow"])
        img.paste(halo_img, (0, 0), halo.point(lambda v: int(v * strength)))

    img.paste(gradient, (0, 0), mask)


def build(theme_name):
    theme = THEMES[theme_name]
    rng = random.Random(SEED)
    img = make_background(theme)
    draw_rain(img, theme, rng)
    draw_vignette(img, theme)
    draw_skewed_band(img, theme)

    add_title(img, theme, TITLE, 88, 146, 8)

    draw = ImageDraw.Draw(img)
    draw_spaced(draw, WIDTH / 2, 200, SUBTITLE, load_font(15), 2, theme["subtitle"])
    draw_spaced(draw, WIDTH / 2, 232, TAGLINE, load_font(12), 1.5, theme["tagline"])

    out = Path(__file__).resolve().parent.parent / "assets" / f"banner-{theme_name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


def main():
    for name in THEMES:
        build(name)


if __name__ == "__main__":
    main()
