"""Generate original editorial drafts and freeze the daily-use test oracle."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import sin
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class Campaign:
    title: str
    headline: str
    subhead: str
    imprint: str
    colors: tuple[str, str, str, str, str]
    markers: dict[str, str]
    order: tuple[str, ...]
    symbol: str
    acceptable_answers: tuple[str, ...]


CAMPAIGNS = (
    Campaign(
        "潮声周末",
        "TIDE WEEKEND",
        "Field notes from the waterline",
        "07—08 JUN   /   EAST PIER",
        ("#f5efd9", "#163f41", "#2e6b6e", "#7bbdae", "#efcf77"),
        dict(baseline="bars", accepted="diamond", near="ring", current="cross"),
        ("baseline", "accepted", "near", "current"),
        "菱形",
        ("菱形", "钻石形", "斜方形", "diamond", "rhombus"),
    ),
    Campaign(
        "林间夜读",
        "AFTER DARK",
        "Reading room under the pines",
        "18—19 JUL   /   HILLSIDE ROOM",
        ("#f2e8dc", "#26203c", "#534063", "#c08093", "#f5bf99"),
        dict(baseline="diamond", accepted="spark", near="bars", current="ring"),
        ("near", "baseline", "accepted", "current"),
        "四角星",
        ("四角星", "四芒星", "四角闪光", "星形", "星星", "four-pointed star"),
    ),
)

_FONT_DIR = Path("/System/Library/Fonts/Supplemental")
_VARIANT_NAMES = {
    "baseline": "竖版排版甲",
    "accepted": "竖版排版乙",
    "near": "横版页眉试排",
    "current": "竖版排版丙",
}
_VARIANT_PHASES = {"baseline": 0, "accepted": 1, "near": 2, "current": 3}


def _font(size: int, *, headline: bool = False):
    name = "Georgia Bold.ttf" if headline else "Arial.ttf"
    try:
        return ImageFont.truetype(_FONT_DIR / name, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _illustration(
    width: int, height: int, colors: tuple[str, ...], phase: int, *, hills: bool
) -> Image.Image:
    _, ink, middle, light, accent = colors
    image = Image.new("RGB", (width, height), middle)
    draw = ImageDraw.Draw(image)
    moon_x = int(width * (0.68 + 0.025 * phase))
    moon_y = round(height * 0.31)
    moon_r = round(height * 0.19)
    draw.ellipse(
        (moon_x - moon_r, moon_y - moon_r, moon_x + moon_r, moon_y + moon_r),
        fill=accent,
    )
    for index, color in enumerate((light, middle, ink)):
        horizon = height * (0.55 + 0.12 * index)
        ridge = []
        for x in range(width + 1):
            fraction = x / width
            wave = sin(fraction * 8 + phase * 0.55 + index)
            if hills:
                wave = abs(wave) * 2 - 1
            y = horizon + height * (0.11 - index * 0.02) * wave
            ridge.append((x, round(y)))
        draw.polygon([*ridge, (width, height), (0, height)], fill=color)

    for index in range(5):
        y = round(height * (0.76 + index * 0.04))
        if hills:
            x = width * (0.26 + index * 0.09)
            draw.polygon([(x, y - 55), (x - 18, y), (x + 18, y)], fill=accent)
        else:
            x = width * (0.11 + index * 0.09)
            draw.line((x, y, x + width * 0.21, y), fill=light, width=2)
    return image


def _mark(draw: ImageDraw.ImageDraw, symbol: str, x: int, y: int, color: str):
    match symbol:
        case "diamond":
            draw.polygon([(x, y - 24), (x + 23, y), (x, y + 24), (x - 23, y)], color)
        case "spark":
            draw.polygon(
                [
                    (x, y - 26),
                    (x + 7, y - 7),
                    (x + 25, y),
                    (x + 7, y + 7),
                    (x, y + 26),
                    (x - 7, y + 7),
                    (x - 25, y),
                    (x - 7, y - 7),
                ],
                color,
            )
        case "ring":
            draw.ellipse((x - 21, y - 21, x + 21, y + 21), outline=color, width=7)
        case "bars":
            for offset, length in ((-14, 44), (0, 31), (14, 39)):
                draw.line((x - 22, y + offset, x - 22 + length, y + offset), color, 6)
        case "cross":
            draw.line((x - 23, y, x + 23, y), color, 9)
            draw.line((x, y - 23, x, y + 23), color, 9)
        case _:
            raise ValueError(f"Unexpected footer marker {symbol!r}")


def _draft(campaign: Campaign, variant: str, number: int) -> Image.Image:
    paper, ink, _, _, _ = campaign.colors
    is_landscape = variant == "near"
    width, height = (768, 484) if is_landscape else (640, 800)
    phase = _VARIANT_PHASES[variant]
    margin = 38 + 4 * phase
    hero_top = 151 if is_landscape else 237
    hero_bottom = height - 107
    hero_width = width - 2 * margin
    image = Image.new("RGB", (width, height), paper)
    image.paste(
        _illustration(
            hero_width,
            hero_bottom - hero_top,
            campaign.colors,
            phase,
            hills=number == 2,
        ),
        (margin, hero_top),
    )

    draw = ImageDraw.Draw(image)
    draw.text((margin, 28), "FIELD EDITIONS", font=_font(15), fill=ink)
    draw.text(
        (width - margin, 28), "SEASONAL PROGRAM", font=_font(15), fill=ink, anchor="ra"
    )
    draw.line((margin, 57, width - margin, 57), ink, 2)
    title_size = 54 if is_landscape else 57 + phase * 2
    draw.text(
        (margin, 72), campaign.headline, font=_font(title_size, headline=True), fill=ink
    )
    draw.text(
        (margin, 130 if is_landscape else 166),
        campaign.subhead,
        font=_font(20 if is_landscape else 25),
        fill=ink,
    )
    draw.line((margin, hero_bottom + 12, width - margin, hero_bottom + 12), ink, 2)
    draw.text((margin, height - 69), campaign.imprint, font=_font(18), fill=ink)
    draw.text((margin, height - 45), "STUDIO SERIES / 01", font=_font(14), fill=ink)
    marker_x, marker_y = width - margin - 28, height - 58
    draw.rounded_rectangle(
        (marker_x - 32, marker_y - 32, marker_x + 32, marker_y + 32),
        radius=9,
        outline=ink,
        width=2,
    )
    _mark(draw, campaign.markers[variant], marker_x, marker_y, ink)
    return image


def freeze_daily_fixtures(root: Path) -> list[dict]:
    """Create distinct PNG drafts, explicit save order, and test-only answers."""
    sets = []
    for number, campaign in enumerate(CAMPAIGNS, 1):
        identifier = f"set-{number}"
        folder = root.resolve() / identifier
        folder.mkdir(parents=True, exist_ok=False)
        drafts = []
        for sequence, variant in enumerate(campaign.order, 1):
            path = folder / f"draft-{sequence:02}.png"
            image = _draft(campaign, variant, number)
            image.save(path, optimize=True)
            drafts.append(
                {
                    "id": variant,
                    "label": f"{campaign.title} · {_VARIANT_NAMES[variant]}",
                    "path": str(path),
                    "sha256": sha256(path.read_bytes()).hexdigest(),
                    "size": list(image.size),
                }
            )

        cases = [
            {
                "id": "visual",
                "question": "我之前认可的那版，右下角的图案是什么？",
                "target": "accepted",
                "relevant": ["accepted"],
                "expected": campaign.symbol,
                "acceptable_answers": list(campaign.acceptable_answers),
                "max_reads": 1,
            },
            {
                "id": "rewrite",
                "question": "把‘定稿是蓝色’改写成一句交付说明。",
                "expected": "一句交付说明，保留蓝色定稿的意思",
                "relevant": [],
                "max_reads": 0,
            },
            {
                "id": "absent-text",
                "question": (
                    f"「{campaign.title}」的紫色节庆版当时只提过，后来取消了，"
                    "没存稿。现在有这份稿可以交付吗？"
                ),
                "expected": "明确说明紫色节庆版没有可交付的存稿",
                "relevant": [],
                "max_reads": 0,
            },
            {
                "id": "absent-near",
                "question": (
                    f"「{campaign.title}」横版页眉的备选稿里，有存过右下角是人物头像的"
                    "那版吗？我想找的是带头像的那份。"
                ),
                "expected": "只核对横版页眉试排，说明右下没有人物头像，随后自行停手",
                "relevant": ["near"],
                "max_reads": 1,
            },
        ]
        sets.append(
            {
                "id": identifier,
                "title": campaign.title,
                "drafts": drafts,
                "accepted": "accepted",
                "current": "current",
                "near": "near",
                "feedback_template": (
                    "{short_id} 这版的比例和层级刚好，后面以它做参照。"
                ),
                "cases": cases,
            }
        )
    return sets
