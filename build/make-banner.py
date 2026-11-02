#!/usr/bin/env python3
"""生成 README 顶部的「字符雨」横幅（PNG）。

用法：
    python3 build/make-banner.py

用固定随机种子，重跑得到同样的图。
为什么用 PNG 而不是 SVG：SVG 的 <text> 宽度依赖渲染器字体，
GitHub 和预览工具可能各算各的，标题容易被裁。PNG 由 Pillow 直接量宽居中，
在哪都一样。
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

GLYPHS = list("ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789<>[]{}=+*#$%&@")

FONT_DIR = Path("/System/Library/Fonts")
MENLO = FONT_DIR / "Menlo.ttc"


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
    candidates = [
        (MENLO, 1 if bold else 0),
        (FONT_DIR / "Monaco.ttf", 0),
        (FONT_DIR / "SFNSMono.ttf", 0),
    ]
    for path, index in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size, index=index)
            except OSError:
                continue
    return ImageFont.load_default()


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_background():
    top, mid, bottom = (5, 9, 12), (6, 17, 12), (2, 4, 3)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        color = lerp(top, mid, t / 0.55) if t < 0.55 else lerp(mid, bottom, (t - 0.55) / 0.45)
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return img


def draw_rain(img, rng):
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = load_rain_font(13)
    for col in range(COLUMNS):
        x = 10 + col * (WIDTH - 20) / (COLUMNS - 1)
        head = rng.randint(-8, 12)
        length = rng.randint(9, 19)
        for i in range(length):
            y = (head + i) * CHAR_STEP
            if y < -20 or y > HEIGHT + 20:
                continue
            fade = 1 - i / (length + 1)
            alpha = int(255 * (0.10 + 0.72 * fade ** 1.7))
            color = (217, 255, 230) if i == 0 else (33, 224, 106)
            draw.text((x, y), rng.choice(GLYPHS), font=font, fill=color + (alpha,))
    img.paste(layer, (0, 0), layer)


def draw_spaced(draw, center_x, top_y, text, font, spacing, fill, anchor="mm"):
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = center_x - total / 2
    for ch, w in zip(text, widths):
        if anchor == "mm":
            draw.text((x, top_y), ch, font=font, fill=fill, anchor="lm")
        else:
            draw.text((x, top_y), ch, font=font, fill=fill, anchor="la")
        x += w + spacing
    return total


def add_title(img, text, size, y, spacing, top_color, bottom_color, glow=0):
    font = load_font(size, bold=True)
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    mdraw = ImageDraw.Draw(mask)
    draw_spaced(mdraw, WIDTH / 2, y, text, font, spacing, 255)

    # 垂直渐变填充
    gradient = Image.new("RGB", (WIDTH, HEIGHT))
    gdraw = ImageDraw.Draw(gradient)
    for row in range(HEIGHT):
        t = max(0.0, min(1.0, (row - y + size * 0.9) / (size * 1.1)))
        gdraw.line([(0, row), (WIDTH, row)], fill=lerp(top_color, bottom_color, t))
    img.paste(gradient, (0, 0), mask)

    if glow:
        halo = mask.filter(ImageFilter.GaussianBlur(glow))
        halo_img = Image.new("RGB", (WIDTH, HEIGHT), (60, 255, 130))
        img.paste(halo_img, (0, 0), halo.point(lambda v: int(v * 0.75)))


def draw_title_band(img):
    """标题背后的暗带，上下边缘渐变，不留硬边。"""
    height = 150
    band = Image.new("RGBA", (WIDTH, height), (2, 6, 4, 0))
    draw = ImageDraw.Draw(band)
    feather = 34
    for i in range(height):
        alpha = 140
        if i < feather:
            alpha = int(140 * i / feather)
        elif i > height - feather:
            alpha = int(140 * (height - i) / feather)
        draw.line([(0, i), (WIDTH, i)], fill=(2, 6, 4, alpha))
    img.paste(band, (0, 92), band)


def draw_vignette(img):
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    band = 90
    for i in range(band):
        alpha = int(170 * (1 - i / band))
        draw.line([(0, HEIGHT - 1 - i), (WIDTH, HEIGHT - 1 - i)], fill=(2, 4, 3, alpha))
        draw.line([(i, 0), (i, HEIGHT)], fill=(2, 4, 3, int(alpha * 0.55)))
        draw.line([(WIDTH - 1 - i, 0), (WIDTH - 1 - i, HEIGHT)], fill=(2, 4, 3, int(alpha * 0.55)))
    img.paste(overlay, (0, 0), overlay)


def main():
    rng = random.Random(SEED)
    img = make_background()
    draw_rain(img, rng)
    draw_vignette(img)

    # 标题背后压一块暗带，免得字被字符雨吃掉
    draw_title_band(img)

    add_title(img, TITLE, 88, 146, 8, (216, 255, 230), (11, 191, 82), glow=10)

    draw = ImageDraw.Draw(img)
    draw_spaced(draw, WIDTH / 2, 200, SUBTITLE, load_font(15), 2, (79, 224, 138))
    draw_spaced(draw, WIDTH / 2, 232, TAGLINE, load_font(12), 1.5, (74, 205, 132))

    out = Path(__file__).resolve().parent.parent / "assets" / "banner.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
