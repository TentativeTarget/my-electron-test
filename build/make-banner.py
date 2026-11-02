#!/usr/bin/env python3
"""生成 README 顶部的横幅：左边白、右边黑，斜切一刀。

用法：
    python3 build/make-banner.py

输出 assets/banner.png（1200×300）。

设计：
- 分界线从底部 x=460 斜到顶部 x=720，左侧纯白、右侧深黑
- 两边各下各的字符雨：白区淡灰、黑区亮蓝
- 标题横跨分界，白区用深蓝、黑区用近白，一个字砍成两半也是两种颜色
- 配色取自 build/icon-source.png：主蓝 #227afc
"""
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1200, 300
COLUMNS = 46
CHAR_STEP = 16
TITLE = "CollabNote"
SUBTITLE = "MULTI-USER NOTES  ·  ELECTRON + PYTHON"
TAGLINE = "share notes  ·  track money  ·  stay in sync"
SEED = 20261015

SPLIT_TOP = 720   # 分界线在顶部的 x
SPLIT_BOTTOM = 460  # 分界线在底部的 x
RAIN_SLOPE = 0.18

GLYPHS = list("ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789<>[]{}=+*#$%&@")

FONT_DIR = Path("/System/Library/Fonts")
MENLO = FONT_DIR / "Menlo.ttc"

# 白区 / 黑区各自的配色
WHITE_BG = ((255, 255, 255), (238, 243, 250))
DARK_BG = ((4, 7, 13), (10, 18, 32))
WHITE_RAIN = (150, 168, 190)
DARK_RAIN = (61, 140, 255)
DARK_RAIN_HEAD = (214, 232, 255)
WHITE_TITLE = (11, 47, 107)
DARK_TITLE = (242, 248, 255)
WHITE_SUB = (43, 98, 184)
DARK_SUB = (143, 192, 255)
WHITE_TAG = (120, 150, 190)
DARK_TAG = (95, 143, 214)
SPLIT_LINE = (34, 122, 252)


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


def vertical_gradient(stops):
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        draw.line([(0, y), (WIDTH, y)], fill=lerp(stops[0], stops[1], t))
    return img


def split_mask():
    """分界线左侧为 255，右侧为 0。"""
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    ImageDraw.Draw(mask).polygon(
        [(0, 0), (SPLIT_TOP, 0), (SPLIT_BOTTOM, HEIGHT), (0, HEIGHT)], fill=255
    )
    return mask


def rain_layer(rng, color, head_color, alpha_scale):
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
            alpha = int(255 * (0.08 + 0.58 * fade ** 1.7) * alpha_scale)
            color_now = head_color if i == 0 else color
            draw.text((x, y), rng.choice(GLYPHS), font=font, fill=color_now + (alpha,))
    return layer.crop((0, 0, WIDTH, HEIGHT))


def clip_to(layer, mask):
    """按 mask 裁掉 alpha。"""
    out = layer.copy()
    out.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    return out


def draw_text_mask(text, size, y, spacing, bold=True):
    font = load_font(size, bold=bold)
    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(mask)
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = WIDTH / 2 - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=255, anchor="lm")
        x += w + spacing
    return mask


def paint_two_tone(img, text_mask, light_fill, dark_fill, region):
    """文字按分界线换色：region 内（左侧）用 dark_fill，外侧用 light_fill。"""
    outside = ImageChops.invert(region)
    for mask, color in ((ImageChops.multiply(text_mask, region), dark_fill),
                        (ImageChops.multiply(text_mask, outside), light_fill)):
        layer = Image.new("RGB", (WIDTH, HEIGHT), color)
        img.paste(layer, (0, 0), mask)


def build():
    rng = random.Random(SEED)
    region = split_mask()

    # 白区黑区分开铺底
    img = vertical_gradient(DARK_BG)
    white = vertical_gradient(WHITE_BG)
    img.paste(white, (0, 0), region)

    # 两边的雨各画一次，再用区域裁掉
    dark_rain = clip_to(rain_layer(rng, DARK_RAIN, DARK_RAIN_HEAD, 1.0), ImageChops.invert(region))
    img.paste(dark_rain, (0, 0), dark_rain)
    rng2 = random.Random(SEED + 7)
    white_rain = clip_to(rain_layer(rng2, WHITE_RAIN, WHITE_RAIN, 0.42), region)
    img.paste(white_rain, (0, 0), white_rain)

    # 分界线：一条细亮线，附一点光晕
    line = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(line)
    ldraw.line([(SPLIT_TOP, 0), (SPLIT_BOTTOM, HEIGHT)], fill=SPLIT_LINE + (220,), width=3)
    glow = line.filter(ImageFilter.GaussianBlur(6))
    img.paste(Image.new("RGB", (WIDTH, HEIGHT), SPLIT_LINE), (0, 0), glow)
    img.paste(line, (0, 0), line)

    # 标题：白字压黑底、深蓝字压白底
    title_mask = draw_text_mask(TITLE, 88, 146, 8)
    paint_two_tone(img, title_mask, DARK_TITLE, WHITE_TITLE, region)

    # 副标题与标语
    sub_mask = draw_text_mask(SUBTITLE, 15, 200, 2, bold=False)
    paint_two_tone(img, sub_mask, DARK_SUB, WHITE_SUB, region)
    tag_mask = draw_text_mask(TAGLINE, 12, 232, 1.5, bold=False)
    paint_two_tone(img, tag_mask, DARK_TAG, WHITE_TAG, region)

    out = Path(__file__).resolve().parent.parent / "assets" / "banner.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
