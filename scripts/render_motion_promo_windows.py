"""
Студийный генератор продающего моушен-ролика «Заявка 24/7» для ниши:
Ремонт и замена окон, замена уплотнителя (резинок), регулировка на Авито.
Формат: 1920x1080 Full HD (16:9), H.264 High 4.1 +faststart — идеально для Telegram, ВК и сайта.

Поддерживает как синтез edge-tts, так и студийную озвучку ElevenLabs (TTS v3)
из папки video_motion_promo_windows/custom_voice/ с автоматической адаптацией анимаций.
"""

import argparse
import asyncio
import hashlib
import math
import os
import struct
import subprocess
import wave
from pathlib import Path
from typing import Callable, List, Tuple

import edge_tts
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
XFADE_SEC = 0.35
SAMPLE_RATE = 48000

OUT_DIR = Path("video_motion_promo_windows")
CACHE_DIR = OUT_DIR / "cache"
QA_DIR = OUT_DIR / "qa_frames"
CUSTOM_VOICE_DIR = OUT_DIR / "custom_voice"

# --- Палитра (Чистый Tech / Winter-Sky & Emerald) ---
BG_BASE = (6, 10, 18)
SURFACE = (13, 20, 34)
SURFACE_ELEV = (20, 31, 52)
BORDER = (38, 56, 84)

EMERALD = (0, 229, 153)
EMERALD_DARK = (10, 42, 34)
CYAN = (14, 198, 255)
BLUE = (59, 130, 246)
RED = (244, 63, 94)
RED_DARK = (44, 14, 24)
AMBER = (245, 158, 11)

WHITE = (248, 250, 252)
MUTED = (148, 163, 184)
FAINT = (94, 110, 136)


def load_font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    candidates: List[str] = []
    win_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if mono:
        candidates += [
            str(win_dir / ("consolab.ttf" if bold else "consola.ttf")),
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ]
    elif bold:
        candidates += [
            str(win_dir / "segoeuib.ttf"),
            str(win_dir / "arialbd.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates += [
            str(win_dir / "segoeui.ttf"),
            str(win_dir / "arial.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


F_HERO = load_font(74, bold=True)
F_H1 = load_font(52, bold=True)
F_H2 = load_font(36, bold=True)
F_H3 = load_font(29, bold=True)
F_BODY = load_font(25, bold=False)
F_BODY_B = load_font(25, bold=True)
F_SMALL = load_font(20, bold=False)
F_BADGE = load_font(22, bold=True)
F_MONO_XL = load_font(84, bold=True, mono=True)
F_MONO_LG = load_font(50, bold=True, mono=True)
F_MONO_MD = load_font(34, bold=True, mono=True)
F_SUB = load_font(33, bold=True)


# --- Математика анимаций ---
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def prog(t: float, start: float, duration: float) -> float:
    if duration <= 0:
        return 1.0 if t >= start else 0.0
    return clamp((t - start) / duration)


def ease_out_cubic(x: float) -> float:
    x = clamp(x)
    return 1.0 - (1.0 - x) ** 3


def ease_out_back(x: float) -> float:
    x = clamp(x)
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * ((x - 1.0) ** 3) + c1 * ((x - 1.0) ** 2)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix_rgb(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = clamp(t)
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


class VisualAssets:
    def __init__(self) -> None:
        self.orb_emerald = self._make_orb((0, 229, 153), alpha=54, size=960)
        self.orb_cyan = self._make_orb((14, 198, 255), alpha=52, size=920)
        self.orb_red = self._make_orb((244, 63, 94), alpha=60, size=960)
        self.grid = self._make_grid(W + 128, H + 128, step=64)

    @staticmethod
    def _make_orb(rgb: Tuple[int, int, int], alpha: int, size: int) -> Image.Image:
        small = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
        d = ImageDraw.Draw(small)
        d.ellipse([36, 36, 124, 124], fill=(rgb[0], rgb[1], rgb[2], alpha))
        small = small.filter(ImageFilter.GaussianBlur(radius=28))
        return small.resize((size, size), Image.Resampling.BILINEAR)

    @staticmethod
    def _make_grid(w: int, h: int, step: int) -> Image.Image:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        col = (255, 255, 255, 10)
        for x in range(0, w, step):
            d.line([(x, 0), (x, h)], fill=col, width=1)
        for y in range(0, h, step):
            d.line([(0, y), (w, y)], fill=col, width=1)
        return img

    def make_bg(self, t: float, mood: str = "emerald") -> Image.Image:
        img = Image.new("RGB", (W, H), BG_BASE)
        dx = int(math.sin(t * 0.55) * 90)
        dy = int(math.cos(t * 0.45) * 55)

        if mood == "red":
            img.paste(self.orb_red, (1050 + dx, -180 + dy), self.orb_red)
            img.paste(self.orb_red, (-280 - dx, 420 - dy), self.orb_red)
        elif mood == "cyan":
            img.paste(self.orb_cyan, (1020 + dx, -180 + dy), self.orb_cyan)
            img.paste(self.orb_emerald, (-240 - dx, 380 - dy), self.orb_emerald)
        else:
            img.paste(self.orb_emerald, (1020 + dx, -200 + dy), self.orb_emerald)
            img.paste(self.orb_cyan, (-240 - dx, 400 - dy), self.orb_cyan)

        gx = -int((t * 14) % 64)
        gy = -int((t * 8) % 64)
        img.paste(self.grid, (gx, gy), self.grid)
        return img


ASSETS = VisualAssets()


# --- Векторные иконки (без эмодзи) ---
def draw_icon(
    d: ImageDraw.ImageDraw,
    kind: str,
    cx: int,
    cy: int,
    r: int,
    color: Tuple[int, int, int],
) -> None:
    w = max(2, r // 6)
    if kind == "window":
        d.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=max(3, r // 5), outline=color, width=w + 1)
        d.line([(cx, cy - r), (cx, cy + r)], fill=color, width=w)
        d.line([(cx - r, cy - int(r * 0.15)), (cx + r, cy - int(r * 0.15))], fill=color, width=w)
        d.line([(cx + int(r * 0.25), cy + int(r * 0.1)), (cx + int(r * 0.25), cy + int(r * 0.48))], fill=color, width=w + 1)
    elif kind == "bolt":
        pts = [
            (cx + int(r * 0.12), cy - r),
            (cx - int(r * 0.55), cy + int(r * 0.08)),
            (cx + int(r * 0.02), cy + int(r * 0.08)),
            (cx - int(r * 0.16), cy + r),
            (cx + int(r * 0.58), cy - int(r * 0.12)),
            (cx - int(r * 0.02), cy - int(r * 0.12)),
        ]
        d.polygon(pts, fill=color)
    elif kind == "clock":
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
        d.line([(cx, cy), (cx, cy - int(r * 0.58))], fill=color, width=w)
        d.line([(cx, cy), (cx + int(r * 0.45), cy + int(r * 0.25))], fill=color, width=w)
    elif kind == "cross":
        s = int(r * 0.65)
        d.line([(cx - s, cy - s), (cx + s, cy + s)], fill=color, width=w + 1)
        d.line([(cx + s, cy - s), (cx - s, cy + s)], fill=color, width=w + 1)
    elif kind == "check":
        pts = [
            (cx - int(r * 0.65), cy + int(r * 0.05)),
            (cx - int(r * 0.15), cy + int(r * 0.55)),
            (cx + int(r * 0.7), cy - int(r * 0.48)),
        ]
        d.line(pts, fill=color, width=w + 2, joint="curve")
    elif kind == "shield":
        pts = [
            (cx, cy - r),
            (cx + int(r * 0.8), cy - int(r * 0.55)),
            (cx + int(r * 0.72), cy + int(r * 0.22)),
            (cx, cy + r),
            (cx - int(r * 0.72), cy + int(r * 0.22)),
            (cx - int(r * 0.8), cy - int(r * 0.55)),
        ]
        d.polygon(pts, outline=color, width=w)
        draw_icon(d, "check", cx, cy, int(r * 0.45), color)
    elif kind == "pause":
        bw = max(4, int(r * 0.28))
        bh = int(r * 0.72)
        gap = int(r * 0.22)
        d.rounded_rectangle([cx - gap - bw, cy - bh, cx - gap, cy + bh], radius=3, fill=color)
        d.rounded_rectangle([cx + gap, cy - bh, cx + gap + bw, cy + bh], radius=3, fill=color)
    elif kind == "send":
        pts = [
            (cx + r, cy - int(r * 0.7)),
            (cx - r, cy - int(r * 0.05)),
            (cx - int(r * 0.15), cy + int(r * 0.2)),
            (cx + int(r * 0.2), cy + int(r * 0.85)),
        ]
        d.polygon(pts, fill=color)
    elif kind == "bell":
        d.pieslice([cx - int(r * 0.7), cy - int(r * 0.85), cx + int(r * 0.7), cy + int(r * 0.55)], 180, 360, fill=color)
        d.rectangle([cx - int(r * 0.7), cy - int(r * 0.15), cx + int(r * 0.7), cy + int(r * 0.45)], fill=color)
        d.rounded_rectangle([cx - int(r * 0.88), cy + int(r * 0.35), cx + int(r * 0.88), cy + int(r * 0.58)], radius=4, fill=color)
        d.ellipse([cx - int(r * 0.22), cy + int(r * 0.55), cx + int(r * 0.22), cy + int(r * 0.92)], fill=color)


def draw_card(d: ImageDraw.ImageDraw, box: List[int], fill=SURFACE, outline=BORDER, width: int = 2, radius: int = 20) -> None:
    x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    if x2 <= x1 + 4 or y2 <= y1 + 4:
        return
    d.rounded_rectangle(
        [x1, y1, x2, y2],
        radius=min(radius, (x2 - x1) // 2, (y2 - y1) // 2),
        fill=fill,
        outline=outline,
        width=width,
    )


def text_size(font: ImageFont.FreeTypeFont, text: str) -> Tuple[int, int]:
    bb = font.getbbox(text)
    return (bb[2] - bb[0], bb[3] - bb[1])


def draw_centered(d: ImageDraw.ImageDraw, text: str, cx: int, y: int, font: ImageFont.FreeTypeFont, fill=WHITE) -> None:
    tw, _ = text_size(font, text)
    d.text((cx - tw // 2, y), text, font=font, fill=fill)


def draw_wrapped(
    d: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_w: int,
    font: ImageFont.FreeTypeFont,
    fill=WHITE,
    line_gap: int = 10,
) -> int:
    curr_y = y
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            tw, _ = text_size(font, test)
            if tw <= max_w or not line:
                line = test
            else:
                d.text((x, curr_y), line, font=font, fill=fill)
                _, th = text_size(font, "Ау")
                curr_y += th + line_gap
                line = word
        if line:
            d.text((x, curr_y), line, font=font, fill=fill)
            _, th = text_size(font, "Ау")
            curr_y += th + line_gap
    return curr_y


def draw_pill(
    d: ImageDraw.ImageDraw,
    text: str,
    cx: int,
    y: int,
    bg=EMERALD_DARK,
    border=EMERALD,
    text_col=EMERALD,
    icon_kind: str = "",
) -> None:
    tw, th = text_size(F_BADGE, text)
    icon_space = 34 if icon_kind else 0
    px, py = 22, 10
    total_w = tw + icon_space + px * 2
    x1 = cx - total_w // 2
    x2 = x1 + total_w
    y2 = y + th + py * 2
    draw_card(d, [x1, y, x2, y2], fill=bg, outline=border, width=2, radius=(y2 - y) // 2)
    tx = x1 + px + icon_space
    if icon_kind:
        draw_icon(d, icon_kind, x1 + px + 10, (y + y2) // 2, 11, text_col)
    d.text((tx, y + py - 2), text, font=F_BADGE, fill=text_col)


def draw_subtitle(d: ImageDraw.ImageDraw, text: str) -> None:
    if not text:
        return
    tw, th = text_size(F_SUB, text)
    px, py = 32, 14
    cx = W // 2
    y = H - 88
    x1 = max(40, cx - tw // 2 - px)
    x2 = min(W - 40, cx + tw // 2 + px)
    draw_card(d, [x1, y - py, x2, y + th + py], fill=(7, 12, 22), outline=BORDER, width=2, radius=16)
    draw_centered(d, text, cx, y - 3, F_SUB, WHITE)


def draw_scene_progress(d: ImageDraw.ImageDraw, global_ratio: float, accent=EMERALD) -> None:
    d.rectangle([0, 0, W, 6], fill=(20, 28, 44))
    bar_w = max(4, int(W * clamp(global_ratio)))
    d.rectangle([0, 0, bar_w, 6], fill=accent)


def active_subtitle(subs: List[Tuple[float, float, str]], t: float, dur: float) -> str:
    ratio = t / max(0.01, dur)
    for start_r, end_r, text in subs:
        if start_r <= ratio <= end_r:
            return text
    return subs[-1][2] if subs else ""


# ==============================================================================
# СЦЕНАРИЙ ДЛЯ ОКОНЩИКОВ (6 сцен, адаптированных под ElevenLabs)
# ==============================================================================
SCENES = [
    {
        "id": "win_sc1_hook",
        "voice": (
            "Двадцать один... восемнадцать. Вы на монтаже, в руках шуруповёрт. "
            "На Авито падает горячая заявка: дует из трёх окон, нужна замена уплотнителя и регулировка. "
            "Клиент ждёт три минуты... и уходит к конкуренту. Заказ ушёл из рук."
        ),
        "subs": [
            (0.0, 0.38, "21:18 • Вы на объекте с инструментом • Пришла заявка на Авито"),
            (0.38, 0.72, "Клиент ждёт ответа 3 минуты и уходит к конкуренту"),
            (0.72, 1.0, "Минус 9 500 – 45 000 ₽ с одного пропущенного чата"),
        ],
    },
    {
        "id": "win_sc2_pain",
        "voice": (
            "До сорока процентов обращений приходят, пока вы на замере, за рулём или уже дома. "
            "Вы платите Авито за просмотры, а мимо кассы утекает до ста восьмидесяти тысяч рублей... каждый месяц."
        ),
        "subs": [
            (0.0, 0.50, "До 40% заявок приходят, пока мастер на объекте или вечером"),
            (0.50, 1.0, "Мимо кассы утекает до 180 000 ₽ каждый месяц"),
        ],
    },
    {
        "id": "win_sc3_solution",
        "voice": (
            "«Заявка двадцать четыре на семь» — ваш умный ИИ-диспетчер для оконного бизнеса. "
            "Он отвечает за пятнадцать секунд в Авито, Ватсапе и Телеграме. "
            "Уточняет число створок, адрес и район, и называет ориентир строго по вашему прайсу."
        ),
        "subs": [
            (0.0, 0.34, "«Заявка 24/7» — умный ИИ-диспетчер для мастеров по окнам"),
            (0.34, 0.65, "Отвечает за 15 секунд в Авито, WhatsApp и Telegram"),
            (0.65, 1.0, "Уточняет створки и район • Строго по вашему прайсу"),
        ],
    },
    {
        "id": "win_sc4_demo",
        "voice": (
            "Смотрите сами: клиент пишет про сквозняк и замену резинок. "
            "Бот за секунды считает объём, называет вилку цен, забирает телефон... "
            "и мгновенно присылает готовую заявку на замер вам в Телеграм."
        ),
        "subs": [
            (0.0, 0.32, "Клиент пишет про сквозняк -> ИИ отвечает за 14 секунд"),
            (0.32, 0.66, "Уточняет число створок, называет вилку и берёт телефон"),
            (0.66, 1.0, "Готовый заказ с районом и номером падает в Telegram"),
        ],
    },
    {
        "id": "win_sc5_roi",
        "voice": (
            "Настройка под ваш прайс занимает десять минут. "
            "Подписка стоит двенадцать девятьсот и полностью окупается... с одной замены окна или двух выездов на ремонт."
        ),
        "subs": [
            (0.0, 0.40, "Настройка под ваш прайс-лист и районы за 10 минут"),
            (0.40, 1.0, "Окупается с 1 замены окна или 2 ремонтов с уплотнителем"),
        ],
    },
    {
        "id": "win_sc6_cta",
        "voice": (
            "Запустите бесплатный тест-драйв на три дня. "
            "Проверьте прямо на ваших реальных клиентах с Авито, сколько заказов бот спасёт уже на этой неделе!"
        ),
        "subs": [
            (0.0, 0.50, "Бесплатный тест-драйв на 3 дня без привязки карты"),
            (0.50, 1.0, "Забирайте все заявки с Авито — даже пока руки в работе"),
        ],
    },
]


def render_sc1(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="red")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, RED)

    draw_pill(
        d,
        "21:18 • МАСТЕР НА ОБЪЕКТЕ / ЗА РУЛЁМ • РУКИ ЗАНЯТЫ",
        W // 2,
        42,
        bg=RED_DARK,
        border=RED,
        text_col=(252, 165, 165),
        icon_kind="clock",
    )

    e_left = ease_out_cubic(prog(t, 0.0, 0.55))
    lx_off = int((1.0 - e_left) * -70)
    lx1, ly1, lx2, ly2 = 80 + lx_off, 126, 980 + lx_off, 916
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    draw_card(d, [lx1, ly1, lx2, ly1 + 78], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    draw_icon(d, "window", lx1 + 42, ly1 + 39, 16, CYAN)
    d.text((lx1 + 74, ly1 + 22), "Авито • Ремонт окон и замена уплотнителя", font=F_H3, fill=WHITE)

    # Сообщение клиента
    e_msg = ease_out_back(prog(t, 2.5, 0.45))
    if e_msg > 0.01:
        my = ly1 + 112 + int((1.0 - e_msg) * 35)
        draw_card(d, [lx1 + 36, my, lx2 - 36, my + 184], fill=(22, 34, 58), outline=(58, 80, 118), width=2, radius=18)
        d.text((lx1 + 64, my + 20), "Клиент на Авито • Срочно", font=F_BODY_B, fill=CYAN)
        draw_wrapped(
            d,
            "Добрый вечер! Сильно дует из 3 пластиковых окон и сломалась ручка на балконе. Сколько стоит поменять резинки и отрегулировать завтра?",
            lx1 + 64,
            my + 62,
            740,
            F_BODY,
            WHITE,
        )

    # Таймер ожидания (бежит с 5.5с до 13.5с)
    p_wait = clamp(prog(t, 5.0, dur * 0.48))
    secs = int(p_wait * 192)
    ty = ly1 + 328
    pulse = 0.5 + 0.5 * math.sin(t * 9.0)
    t_border = mix_rgb((140, 35, 55), RED, pulse)
    draw_card(d, [lx1 + 36, ty, lx2 - 36, ty + 168], fill=RED_DARK, outline=t_border, width=2, radius=18)
    draw_icon(d, "clock", lx1 + 82, ty + 54, 22, RED)
    d.text((lx1 + 122, ty + 32), f"Клиент ждёт ответ: 0{secs // 60}:{secs % 60:02d}", font=F_H2, fill=(254, 202, 202))

    bar_x1, bar_y1, bar_x2, bar_y2 = lx1 + 64, ty + 104, lx2 - 64, ty + 124
    draw_card(d, [bar_x1, bar_y1, bar_x2, bar_y2], fill=(28, 12, 20), outline=RED_DARK, width=1, radius=10)
    fill_w = max(14, int((bar_x2 - bar_x1) * p_wait))
    draw_card(d, [bar_x1, bar_y1, bar_x1 + fill_w, bar_y2], fill=RED, outline=RED, width=1, radius=10)
    d.text((lx1 + 64, ty + 134), "Вы на монтаже или за рулём — телефон в кармане", font=F_SMALL, fill=MUTED)

    # Алерт об уходе клиента
    e_left_chat = ease_out_back(prog(t, dur * 0.65, 0.42))
    if e_left_chat > 0.01:
        ly_alert = ly1 + 528 + int((1.0 - e_left_chat) * 40)
        draw_card(d, [lx1 + 36, ly_alert, lx2 - 36, ly_alert + 104], fill=(55, 16, 28), outline=RED, width=3, radius=18)
        draw_icon(d, "cross", lx1 + 82, ly_alert + 52, 20, RED)
        d.text((lx1 + 122, ly_alert + 34), "Клиент вызвал другого мастера с Авито", font=F_H3, fill=(254, 202, 202))

    # Правая панель (потеря заказа)
    e_right = ease_out_cubic(prog(t, 0.15, 0.55))
    rx_off = int((1.0 - e_right) * 70)
    rx1, ry1, rx2, ry2 = 1020 + rx_off, 126, W - 80 + rx_off, 916
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(20, 12, 22), outline=RED, width=3, radius=24)
    d.text((rx1 + 48, ry1 + 50), "ЗАКАЗ УШЁЛ КОНКУРЕНТУ", font=F_BADGE, fill=(252, 165, 165))

    p_loss = ease_out_cubic(prog(t, dur * 0.70, 0.65))
    loss_val = int(14500 * p_loss)
    d.text((rx1 + 48, ry1 + 105), f"-{loss_val:,} ₽".replace(",", " "), font=F_MONO_XL, fill=RED)
    d.text((rx1 + 48, ry1 + 206), "а на замене окна под ключ — минус 45 000 ₽", font=F_BODY_B, fill=(252, 165, 165))

    draw_wrapped(
        d,
        "Когда дома дует или не закрывается створка — клиент пишет 4–5 мастерам сразу.\nВыезжает тот, кто ответил за 1–2 минуты.",
        rx1 + 48,
        ry1 + 275,
        720,
        F_H3,
        WHITE,
        line_gap=12,
    )

    facts = [
        ("Ремонт и резинки", "чек 4 500 – 15 000 ₽: теряется за 3 минуты тишины"),
        ("Замена стеклопакета / окна", "чек 18 000 – 85 000 ₽: забирает самый быстрый"),
        ("Реклама на Авито впустую", "вы оплатили просмотр, а заказ забрал сосед"),
    ]
    for i, (k, desc) in enumerate(facts):
        ef = ease_out_cubic(prog(t, 0.8 + i * 0.45, 0.45))
        if ef <= 0.01:
            continue
        fy = ry1 + 448 + i * 106 + int((1.0 - ef) * 28)
        draw_card(d, [rx1 + 48, fy, rx2 - 48, fy + 88], fill=(32, 16, 28), outline=(76, 30, 44), width=1, radius=14)
        d.text((rx1 + 74, fy + 12), k, font=F_H3, fill=RED)
        d.text((rx1 + 74, fy + 50), desc, font=F_SMALL, fill=MUTED)

    draw_subtitle(d, active_subtitle(SCENES[0]["subs"], t, dur))
    return img


def render_sc2(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="red")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, RED)

    draw_pill(d, "СКОЛЬКО ЗАЯВОК УТЕКАЕТ, ПОКА ВЫ НА ЗАМЕРАХ", W // 2, 42, bg=RED_DARK, border=RED, text_col=(252, 165, 165), icon_kind="cross")

    lx1, ly1, lx2, ly2 = 80, 126, 980, 916
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    draw_card(d, [lx1, ly1, lx2, ly1 + 74], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    d.text((lx1 + 36, ly1 + 20), "Типовые пропущенные заявки на Авито", font=F_H3, fill=WHITE)

    missed = [
        ("Замена уплотнителя • 14:20", "4 створки (Rehau), поменять резинки + смазать фурнитуру.", "-9 600 ₽"),
        ("Заклинила створка • 18:45", "Балконная дверь не закрывается до конца, дует. Приедете?", "-6 500 ₽"),
        ("Замена стеклопакета • 20:10", "Треснуло внутреннее стекло в спальне, 2-камерный пакет.", "-14 000 ₽"),
        ("Замена окон под ключ • 22:30", "Нужно поменять 2 окна в двушке (кухня + зал), цена?", "-68 000 ₽"),
    ]
    for i, (head, q, loss) in enumerate(missed):
        em = ease_out_back(prog(t, 0.2 + i * (dur * 0.12), 0.45))
        if em <= 0.01:
            continue
        x_shift = int((1.0 - em) * -60)
        cy1 = ly1 + 102 + i * 165
        draw_card(d, [lx1 + 32 + x_shift, cy1, lx2 - 32 + x_shift, cy1 + 142], fill=(24, 17, 30), outline=(120, 38, 56), width=2, radius=16)
        d.text((lx1 + 58 + x_shift, cy1 + 18), head, font=F_BODY_B, fill=CYAN)
        d.text((lx2 - 220 + x_shift, cy1 + 16), loss, font=F_H3, fill=RED)
        draw_wrapped(d, q, lx1 + 58 + x_shift, cy1 + 60, 760, F_BODY, WHITE)
        d.text((lx1 + 58 + x_shift, cy1 + 106), "Без ответа > 10 минут • заявка ушла другому мастеру", font=F_SMALL, fill=(252, 165, 165))

    rx1, ry1, rx2, ry2 = 1020, 126, W - 80, 916
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(20, 12, 22), outline=RED, width=3, radius=24)
    d.text((rx1 + 48, ry1 + 52), "МИМО КАССЫ КАЖДЫЙ МЕСЯЦ", font=F_BADGE, fill=(252, 165, 165))

    p_cnt = ease_out_cubic(prog(t, dur * 0.45, dur * 0.48))
    val = int(180000 * p_cnt)
    d.text((rx1 + 48, ry1 + 108), f"-{val:,} ₽".replace(",", " "), font=F_MONO_XL, fill=RED)

    d.text((rx1 + 48, ry1 + 250), "Заявок приходит на замерах и вечером:", font=F_BODY_B, fill=WHITE)
    p_bar = ease_out_cubic(prog(t, 0.4, dur * 0.35))
    pct = int(40 * p_bar)
    d.text((rx2 - 150, ry1 + 244), f"{pct}%", font=F_MONO_MD, fill=RED)

    bx1, by1, bx2, by2 = rx1 + 48, ry1 + 302, rx2 - 48, ry1 + 332
    draw_card(d, [bx1, by1, bx2, by2], fill=(35, 16, 26), outline=(80, 30, 44), width=1, radius=14)
    draw_card(d, [bx1, by1, bx1 + max(18, int((bx2 - bx1) * 0.40 * p_bar)), by2], fill=RED, outline=RED, width=1, radius=14)

    draw_wrapped(
        d,
        "В сезон и в непогоду чаты разрываются.\nПока вы крутите фурнитуру или едете по пробкам, горячие заявки забирают конкуренты.",
        rx1 + 48,
        ry1 + 388,
        720,
        F_H3,
        MUTED,
        line_gap=14,
    )

    draw_card(d, [rx1 + 48, ry1 + 590, rx2 - 48, ry1 + 730], fill=(32, 16, 28), outline=RED, width=2, radius=18)
    d.text((rx1 + 76, ry1 + 616), "Даже 2–3 пропущенных заказа в неделю:", font=F_BODY_B, fill=(252, 165, 165))
    d.text((rx1 + 76, ry1 + 662), "это 10–15 выездов и 120–180 тыс. ₽ мимо вас", font=F_BODY, fill=WHITE)

    draw_subtitle(d, active_subtitle(SCENES[1]["subs"], t, dur))
    return img


def render_sc3(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="emerald")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, EMERALD)

    draw_pill(d, "РЕШЕНИЕ • ИИ-ДИСПЕТЧЕР ДЛЯ ОКОННОГО БИЗНЕСА", W // 2, 42, bg=EMERALD_DARK, border=EMERALD, text_col=EMERALD, icon_kind="window")

    hub_y = 300
    hub_cx = W // 2

    left_nodes = [
        ("Авито Pro", "Ремонт и монтаж", (0, 170, 255), 175),
        ("WhatsApp", "Чат с клиентами", (37, 211, 102), 300),
        ("Telegram", "Личный бот", (56, 189, 248), 425),
    ]
    right_nodes = [
        ("Telegram мастера", "Карточка выезда", EMERALD, 175),
        ("График замеров", "Район + время", (76, 139, 245), 300),
        ("amoCRM / Б24", "Готовая сделка", CYAN, 425),
    ]

    for i, (title, sub, col, ny) in enumerate(left_nodes):
        d.line([(470, ny), (755, hub_y)], fill=(38, 62, 74), width=3)
        p_dot = (t * 0.9 + i * 0.33) % 1.0
        px = int(lerp(470, 755, p_dot))
        py = int(lerp(ny, hub_y, p_dot))
        d.ellipse([px - 7, py - 7, px + 7, py + 7], fill=col)

        en = ease_out_cubic(prog(t, 0.1 + i * 0.12, 0.45))
        ox = int((1.0 - en) * -50)
        draw_card(d, [110 + ox, ny - 44, 470 + ox, ny + 44], fill=SURFACE, outline=col, width=2, radius=16)
        d.ellipse([136 + ox, ny - 10, 156 + ox, ny + 10], fill=col)
        d.text((174 + ox, ny - 26), title, font=F_H3, fill=WHITE)
        d.text((174 + ox, ny + 10), sub, font=F_SMALL, fill=MUTED)

    for i, (title, sub, col, ny) in enumerate(right_nodes):
        d.line([(1165, hub_y), (1450, ny)], fill=(38, 62, 74), width=3)
        p_dot = (t * 0.9 + i * 0.33 + 0.5) % 1.0
        px = int(lerp(1165, 1450, p_dot))
        py = int(lerp(hub_y, ny, p_dot))
        d.ellipse([px - 7, py - 7, px + 7, py + 7], fill=EMERALD)

        en = ease_out_cubic(prog(t, dur * 0.28 + i * 0.12, 0.45))
        ox = int((1.0 - en) * 50)
        draw_card(d, [1450 + ox, ny - 44, 1810 + ox, ny + 44], fill=SURFACE, outline=col, width=2, radius=16)
        d.ellipse([1476 + ox, ny - 10, 1496 + ox, ny + 10], fill=col)
        d.text((1514 + ox, ny - 26), title, font=F_H3, fill=WHITE)
        d.text((1514 + ox, ny + 10), sub, font=F_SMALL, fill=MUTED)

    pulse = 0.5 + 0.5 * math.sin(t * 4.5)
    core_border = mix_rgb((0, 175, 120), EMERALD, pulse)
    draw_card(d, [hub_cx - 210, hub_y - 125, hub_cx + 210, hub_y + 125], fill=EMERALD_DARK, outline=core_border, width=4, radius=28)
    draw_icon(d, "window", hub_cx, hub_y - 66, 26, EMERALD)
    draw_centered(d, "Заявка 24/7", hub_cx, hub_y - 18, F_H1, WHITE)
    draw_centered(d, "ИИ-мастер по окнам", hub_cx, hub_y + 46, F_BODY, EMERALD)
    draw_centered(d, "Ответ 15 сек • Без выходных", hub_cx, hub_y + 82, F_SMALL, MUTED)

    cards = [
        ("bolt", "Пока руки заняты", "Отвечает за 15 секунд, уточняет число створок, проблему и район выезда.", EMERALD),
        ("shield", "Строго ваш прайс", "Называет только ваши расценки на резинки, регулировку и монтаж. Без ошибок.", CYAN),
        ("pause", "Умная автопауза", "Зашли в чат сами — бот мгновенно замолкает и не мешает живому общению.", AMBER),
    ]
    cw = 560
    gap = 40
    start_x = (W - (cw * 3 + gap * 2)) // 2
    for i, (ico, title, desc, col) in enumerate(cards):
        ec = ease_out_back(prog(t, dur * 0.55 + i * 0.35, 0.5))
        if ec <= 0.01:
            continue
        cx1 = start_x + i * (cw + gap)
        cy1 = 535 + int((1.0 - ec) * 55)
        draw_card(d, [cx1, cy1, cx1 + cw, cy1 + 355], fill=SURFACE, outline=col, width=2, radius=22)
        draw_card(d, [cx1 + 34, cy1 + 34, cx1 + 106, cy1 + 106], fill=SURFACE_ELEV, outline=col, width=2, radius=18)
        draw_icon(d, ico, cx1 + 70, cy1 + 70, 22, col)
        d.text((cx1 + 34, cy1 + 132), title, font=F_H2, fill=WHITE)
        draw_wrapped(d, desc, cx1 + 34, cy1 + 190, cw - 68, F_BODY, MUTED, line_gap=10)

    draw_subtitle(d, active_subtitle(SCENES[2]["subs"], t, dur))
    return img


def render_sc4(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="cyan")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, CYAN)

    draw_pill(d, "ЖИВОЙ ПРИМЕР • ЧАТ АВИТО -> ЗАЯВКА МАСТЕРУ В TELEGRAM", W // 2, 42, bg=(10, 32, 46), border=CYAN, text_col=CYAN, icon_kind="send")

    lx1, ly1, lx2, ly2 = 80, 122, 1000, 918
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    draw_card(d, [lx1, ly1, lx2, ly1 + 76], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    d.ellipse([lx1 + 28, ly1 + 26, lx1 + 48, ly1 + 46], fill=EMERALD)
    d.text((lx1 + 64, ly1 + 20), "Оконный мастер «Заявка 24/7» • Онлайн", font=F_H3, fill=WHITE)

    # 1. Клиент пишет
    e1 = ease_out_back(prog(t, 0.2, 0.45))
    if e1 > 0.01:
        y1 = ly1 + 100 + int((1.0 - e1) * 30)
        draw_card(d, [lx1 + 32, y1, lx2 - 110, y1 + 130], fill=(24, 38, 66), outline=(56, 86, 130), width=2, radius=18)
        d.text((lx1 + 56, y1 + 16), "Клиент (Елена) • 21:18", font=F_SMALL, fill=CYAN)
        draw_wrapped(d, "Дует из 3 пластиковых окон, резинки высохли + ручка туго идёт. Сколько стоит сделать?", lx1 + 56, y1 + 48, 720, F_BODY, WHITE)

    # 2. Индикатор печати
    p_start = dur * 0.16
    p_end = dur * 0.32
    if p_start <= t < p_end:
        ty = ly1 + 256
        draw_card(d, [lx1 + 140, ty, lx1 + 470, ty + 66], fill=EMERALD_DARK, outline=EMERALD, width=1, radius=18)
        for idx in range(3):
            bounce = int(abs(math.sin(t * 7.5 - idx * 0.7)) * 8)
            dx = lx1 + 174 + idx * 20
            dy = ty + 35 - bounce
            d.ellipse([dx - 5, dy - 5, dx + 5, dy + 5], fill=EMERALD)
        d.text((lx1 + 240, ty + 18), "ИИ считает по прайсу...", font=F_SMALL, fill=EMERALD)

    # 3. Ответ ИИ
    e2 = ease_out_back(prog(t, dur * 0.32, 0.45))
    if e2 > 0.01:
        y2 = ly1 + 254 + int((1.0 - e2) * 30)
        draw_card(d, [lx1 + 90, y2, lx2 - 32, y2 + 236], fill=EMERALD_DARK, outline=EMERALD, width=2, radius=18)
        d.text((lx1 + 118, y2 + 16), "ИИ-ассистент мастера • 21:18 (14 сек)", font=F_SMALL, fill=EMERALD)
        draw_wrapped(
            d,
            "Добрый вечер! Замена уплотнителя (EPDM Германия) — 350 ₽/м.п., регулировка + смазка — 800 ₽/створка. На 3 окна ориентир 7 500 – 9 500 ₽ под ключ с гарантией 2 года.\nВ каком вы районе? Оставьте номер — мастер согласует выезд на завтра!",
            lx1 + 118,
            y2 + 48,
            740,
            F_BODY,
            WHITE,
            line_gap=7,
        )

    # 4. Клиент даёт телефон
    e3 = ease_out_back(prog(t, dur * 0.52, 0.45))
    if e3 > 0.01:
        y3 = ly1 + 514 + int((1.0 - e3) * 30)
        draw_card(d, [lx1 + 32, y3, lx2 - 110, y3 + 118], fill=(24, 38, 66), outline=EMERALD, width=2, radius=18)
        d.text((lx1 + 56, y3 + 16), "Клиент (Елена) • 21:19", font=F_SMALL, fill=CYAN)
        d.text((lx1 + 56, y3 + 52), "ул. Мира, завтра после 14:00. +7 (916) 442-18-90", font=F_BODY_B, fill=EMERALD)

    # 5. Бейдж перехвата
    e4 = ease_out_cubic(prog(t, dur * 0.62, 0.4))
    if e4 > 0.01:
        y4 = ly1 + 656 + int((1.0 - e4) * 20)
        draw_card(d, [lx1 + 32, y4, lx2 - 32, y4 + 78], fill=(12, 36, 30), outline=EMERALD, width=2, radius=14)
        draw_icon(d, "check", lx1 + 74, y4 + 39, 16, EMERALD)
        d.text((lx1 + 108, y4 + 23), "Контакт и адрес собраны -> пуш мастеру за 1 сек", font=F_BODY_B, fill=EMERALD)

    # Правая панель: Telegram мастера
    rx1, ry1, rx2, ry2 = 1040, 122, W - 80, 918
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(10, 18, 32), outline=BORDER, width=2, radius=24)
    draw_card(d, [rx1, ry1, rx2, ry1 + 76], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    draw_icon(d, "bell", rx1 + 42, ry1 + 38, 16, EMERALD)
    d.text((rx1 + 74, ry1 + 20), "Телефон мастера (Telegram / CRM)", font=F_H3, fill=WHITE)

    t_tg = dur * 0.68
    if t < t_tg:
        draw_centered(d, "Идёт квалификация заявки...", (rx1 + rx2) // 2, ry1 + 360, F_H3, FAINT)
    else:
        etg = ease_out_back(prog(t, t_tg, 0.5))
        tgy = ry1 + 104 + int((1.0 - etg) * -50)
        draw_card(d, [rx1 + 32, tgy, rx2 - 32, tgy + 382], fill=(10, 32, 30), outline=EMERALD, width=3, radius=20)
        draw_pill(d, "НОВАЯ ЗАЯВКА • ОКНА", rx1 + 195, tgy + 22, bg=EMERALD, border=EMERALD, text_col=(6, 9, 16))
        d.text((rx1 + 58, tgy + 84), "+7 (916) 442-18-90", font=F_MONO_LG, fill=WHITE)
        rows = [
            ("Объём:", "3 окна ПВХ • замена резинок + регул."),
            ("Адрес:", "ул. Мира • завтра после 14:00"),
            ("Ориентир:", "7 500 – 9 500 ₽"),
            ("Клиент:", "Елена • ждёт звонка мастера"),
        ]
        for idx, (k, v) in enumerate(rows):
            ry_row = tgy + 162 + idx * 46
            d.text((rx1 + 58, ry_row), k, font=F_BODY, fill=MUTED)
            d.text((rx1 + 215, ry_row), v, font=F_BODY_B, fill=WHITE if idx != 2 else EMERALD)

    ecrm = ease_out_back(prog(t, dur * 0.82, 0.5))
    if ecrm > 0.01:
        cy = ry1 + 512 + int((1.0 - ecrm) * 50)
        draw_card(d, [rx1 + 32, cy, rx2 - 32, cy + 232], fill=(18, 28, 48), outline=CYAN, width=2, radius=20)
        draw_icon(d, "check", rx1 + 74, cy + 48, 18, CYAN)
        d.text((rx1 + 108, cy + 28), "Ни одного пустого выезда", font=F_H2, fill=CYAN)
        d.text((rx1 + 58, cy + 92), "Заявка готова к звонку + ссылка на чат Авито", font=F_BODY_B, fill=WHITE)
        d.text((rx1 + 58, cy + 136), "Можно выгружать в amoCRM / Битрикс24", font=F_BODY, fill=EMERALD)
        d.text((rx1 + 58, cy + 178), "Освободились с монтажа — просто позвонили и поехали", font=F_SMALL, fill=MUTED)

    draw_subtitle(d, active_subtitle(SCENES[3]["subs"], t, dur))
    return img


def render_sc5(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="emerald")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, EMERALD)

    draw_pill(d, "ОКУПАЕМОСТЬ • 1 ЗАМЕНА ОКНА ИЛИ 2 РЕМОНТА", W // 2, 42, bg=EMERALD_DARK, border=EMERALD, text_col=EMERALD, icon_kind="check")

    lx1, ly1, lx2, ly2 = 80, 126, 980, 916
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    d.text((lx1 + 48, ly1 + 44), "Стоимость сервиса vs спасённые заказы", font=F_H2, fill=WHITE)

    base_y = ly2 - 120
    d.line([(lx1 + 60, base_y), (lx2 - 60, base_y)], fill=BORDER, width=3)

    # Столбец 1: 12 900 ₽
    p1 = ease_out_cubic(prog(t, 0.2, 0.8))
    h1 = max(16, int(135 * p1))
    b1x1, b1x2 = lx1 + 95, lx1 + 310
    draw_card(d, [b1x1, base_y - h1, b1x2, base_y], fill=BLUE, outline=CYAN, width=2, radius=14)
    draw_centered(d, "12 900 ₽", (b1x1 + b1x2) // 2, base_y - h1 - 48, F_H2, CYAN)
    draw_centered(d, "Тариф «Старт»", (b1x1 + b1x2) // 2, base_y + 20, F_BODY_B, WHITE)
    draw_centered(d, "на весь месяц", (b1x1 + b1x2) // 2, base_y + 54, F_SMALL, MUTED)

    # Столбец 2: 18 000 ₽
    p2 = ease_out_back(prog(t, dur * 0.35, 0.8))
    h2 = max(16, int(215 * clamp(p2, 0.0, 1.05)))
    b2x1, b2x2 = lx1 + 350, lx1 + 575
    draw_card(d, [b2x1, base_y - h2, b2x2, base_y], fill=CYAN, outline=WHITE, width=2, radius=14)
    draw_centered(d, "18 000 ₽", (b2x1 + b2x2) // 2, base_y - h2 - 48, F_H2, CYAN)
    draw_centered(d, "2 ремонта", (b2x1 + b2x2) // 2, base_y + 20, F_BODY_B, WHITE)
    draw_centered(d, "резинки + фурнит.", (b2x1 + b2x2) // 2, base_y + 54, F_SMALL, MUTED)

    # Столбец 3: 45 000 ₽
    p3 = ease_out_back(prog(t, dur * 0.52, 0.9))
    h3 = max(16, int(465 * clamp(p3, 0.0, 1.05)))
    b3x1, b3x2 = lx1 + 615, lx1 + 835
    draw_card(d, [b3x1, base_y - h3, b3x2, base_y], fill=EMERALD, outline=(167, 243, 208), width=2, radius=16)
    draw_centered(d, "45 000 ₽", (b3x1 + b3x2) // 2, base_y - h3 - 54, F_H1, EMERALD)
    draw_centered(d, "1 замена окна", (b3x1 + b3x2) // 2, base_y + 20, F_BODY_B, WHITE)
    draw_centered(d, "монтаж под ключ", (b3x1 + b3x2) // 2, base_y + 54, F_SMALL, MUTED)

    # Правая панель
    rx1, ry1, rx2, ry2 = 1020, 126, W - 80, 916
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(10, 28, 26), outline=EMERALD, width=3, radius=24)
    d.text((rx1 + 48, ry1 + 48), "ЧИСТАЯ ПРИБЫЛЬ С 1 ЗАМЕНЫ ОКНА", font=F_BADGE, fill=EMERALD)

    p_profit = ease_out_cubic(prog(t, dur * 0.55, 0.8))
    profit = int(32100 * p_profit)
    d.text((rx1 + 48, ry1 + 98), f"+{profit:,} ₽".replace(",", " "), font=F_MONO_XL, fill=WHITE)
    d.text((rx1 + 48, ry1 + 204), "остальные 20–40 заказов за месяц — целиком в плюс", font=F_BODY, fill=EMERALD)

    es = ease_out_back(prog(t, dur * 0.58, 0.5))
    if es > 0.01:
        sy = ry1 + 282 + int((1.0 - es) * 35)
        draw_card(d, [rx1 + 48, sy, rx2 - 48, sy + 110], fill=EMERALD, outline=WHITE, width=2, radius=18)
        draw_centered(d, "ОКУПАЕТСЯ С 1 ЗАКАЗА", (rx1 + rx2) // 2, sy + 30, F_H2, (6, 9, 16))

    steps = [
        ("01", "Скидываете ссылку на объявление Авито и цены"),
        ("02", "Обучаем бота под ваш прайс и районы за 10 минут"),
        ("03", "Принимаете готовые заявки в Telegram 24/7"),
    ]
    for i, (num, txt) in enumerate(steps):
        ei = ease_out_cubic(prog(t, 0.4 + i * (dur * 0.18), 0.45))
        if ei <= 0.01:
            continue
        iy = ry1 + 445 + i * 102 + int((1.0 - ei) * 24)
        draw_card(d, [rx1 + 48, iy, rx2 - 48, iy + 84], fill=SURFACE, outline=BORDER, width=1, radius=14)
        draw_card(d, [rx1 + 66, iy + 16, rx1 + 122, iy + 68], fill=EMERALD_DARK, outline=EMERALD, width=1, radius=10)
        draw_centered(d, num, rx1 + 94, iy + 26, F_BODY_B, EMERALD)
        d.text((rx1 + 144, iy + 26), txt, font=F_BODY, fill=WHITE)

    draw_subtitle(d, active_subtitle(SCENES[4]["subs"], t, dur))
    return img


def render_sc6(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="emerald")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, EMERALD)

    draw_pill(d, "ЗАПУСК ЗА 10 МИНУТ • БЕЗ ПРИВЯЗКИ КАРТЫ", W // 2, 42, bg=EMERALD_DARK, border=EMERALD, text_col=EMERALD, icon_kind="window")

    cx1, cy1, cx2, cy2 = 160, 122, W - 160, 916
    draw_card(d, [cx1, cy1, cx2, cy2], fill=SURFACE, outline=EMERALD, width=3, radius=30)

    e_logo = ease_out_back(prog(t, 0.0, 0.5))
    ly = cy1 + 44 + int((1.0 - e_logo) * -25)
    draw_card(d, [W // 2 - 42, ly, W // 2 + 42, ly + 84], fill=EMERALD, outline=WHITE, width=2, radius=22)
    draw_icon(d, "window", W // 2, ly + 42, 24, (6, 9, 16))
    draw_centered(d, "Заявка 24/7", W // 2, ly + 100, F_HERO, WHITE)
    draw_centered(d, "ИИ-диспетчер для мастеров и компаний по ремонту и замене окон", W // 2, ly + 192, F_H3, CYAN)

    pulse = 0.5 + 0.5 * math.sin(t * 5.0)
    btn_fill = mix_rgb((0, 200, 134), (45, 245, 175), pulse)
    bw = 860
    bx1, by1, bx2, by2 = W // 2 - bw // 2, cy1 + 312, W // 2 + bw // 2, cy1 + 432
    draw_card(d, [bx1, by1, bx2, by2], fill=btn_fill, outline=WHITE, width=3, radius=24)

    sheen_p = (t * 0.75) % 1.4
    if sheen_p <= 1.0:
        sx = int(lerp(bx1 + 20, bx2 - 90, sheen_p))
        d.rounded_rectangle([sx, by1 + 6, sx + 70, by2 - 6], radius=16, fill=(195, 255, 232))

    draw_centered(d, "ТЕСТ-ДРАЙВ 3 ДНЯ БЕСПЛАТНО", W // 2, by1 + 34, F_H1, (6, 9, 16))

    guarantees = [
        "Под ваш прайс: резинки, регулировка, монтаж",
        "Официальный Авито API + WhatsApp + Telegram",
        "Первые спасённые заявки уже на этой неделе",
    ]
    gx_start = cx1 + 70
    col_w = (cx2 - cx1 - 140) // 3
    for i, gtxt in enumerate(guarantees):
        eg = ease_out_cubic(prog(t, dur * 0.22 + i * 0.18, 0.45))
        if eg <= 0.01:
            continue
        gx = gx_start + i * col_w
        gy = cy1 + 474 + int((1.0 - eg) * 20)
        draw_icon(d, "check", gx + 20, gy + 18, 14, EMERALD)
        draw_wrapped(d, gtxt, gx + 48, gy, col_w - 64, F_BODY_B, WHITE, line_gap=6)

    link_y = cy1 + 602
    draw_card(d, [cx1 + 80, link_y, cx2 - 80, link_y + 132], fill=SURFACE_ELEV, outline=CYAN, width=2, radius=20)
    draw_icon(d, "send", cx1 + 135, link_y + 66, 24, EMERALD)
    d.text((cx1 + 182, link_y + 22), f"Проверить демо в Telegram: {cfg['bot']}", font=F_H2, fill=EMERALD)
    d.text((cx1 + 182, link_y + 76), f"Сайт и подключение: {cfg['site']}", font=F_BODY, fill=CYAN)

    draw_subtitle(d, active_subtitle(SCENES[5]["subs"], t, dur))
    return img


RENDERERS: dict[str, Callable] = {
    "win_sc1_hook": render_sc1,
    "win_sc2_pain": render_sc2,
    "win_sc3_solution": render_sc3,
    "win_sc4_demo": render_sc4,
    "win_sc5_roi": render_sc5,
    "win_sc6_cta": render_sc6,
}


# ==============================================================================
# АУДИО-ИНЖИНИРИНГ
# ==============================================================================
def write_wav(path: Path, samples: List[float], sr: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        buf = bytearray()
        for s in samples:
            buf.extend(struct.pack("<h", int(clamp(s, -1.0, 1.0) * 32767)))
        wf.writeframes(buf)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "r") as wf:
        return wf.getnframes() / float(wf.getframerate())


def build_sfx_library(out_dir: Path) -> None:
    sr = SAMPLE_RATE

    pop = [
        math.sin(2 * math.pi * (620 - 180 * (i / (0.14 * sr))) * (i / sr)) * math.exp(-(i / sr) * 28) * 0.32
        for i in range(int(0.14 * sr))
    ]
    write_wav(out_dir / "sfx_pop.wav", pop)

    dur_w = 0.42
    whoosh = [
        math.sin(2 * math.pi * (160 + 340 * math.sin(math.pi * (i / (dur_w * sr)))) * (i / sr))
        * (math.sin(math.pi * (i / (dur_w * sr))) ** 2) * 0.24
        for i in range(int(dur_w * sr))
    ]
    write_wav(out_dir / "sfx_whoosh.wav", whoosh)

    dur_l = 0.50
    lost = [
        (
            math.sin(2 * math.pi * (240 - 110 * (i / (dur_l * sr))) * (i / sr))
            + 0.35 * math.sin(2 * math.pi * (360 - 165 * (i / (dur_l * sr))) * (i / sr))
        )
        * math.exp(-(i / sr) * 5.5) * 0.28
        for i in range(int(dur_l * sr))
    ]
    write_wav(out_dir / "sfx_lost.wav", lost)

    tg = []
    for i in range(int(0.45 * sr)):
        t = i / sr
        if t < 0.12:
            v = math.sin(2 * math.pi * 880 * t) * math.exp(-t * 12) * 0.34
        else:
            t2 = t - 0.12
            v = math.sin(2 * math.pi * 1318.5 * t2) * math.exp(-t2 * 9) * 0.34
        tg.append(v)
    write_wav(out_dir / "sfx_tg.wav", tg)

    cash = []
    for i in range(int(0.65 * sr)):
        t = i / sr
        env = math.exp(-t * 6.0)
        v = (
            math.sin(2 * math.pi * 523.25 * t)
            + 0.8 * math.sin(2 * math.pi * 659.25 * t)
            + 0.7 * math.sin(2 * math.pi * 783.99 * t)
            + 0.5 * math.sin(2 * math.pi * 1046.50 * t)
        ) * 0.12 * env
        cash.append(v)
    write_wav(out_dir / "sfx_cash.wav", cash)


def build_ambient_pad(path: Path, duration: float) -> None:
    sr = SAMPLE_RATE
    total = int(duration * sr)
    chords = [
        (110.00, 164.81, 220.00, 261.63),
        (87.31, 130.81, 174.61, 220.00),
        (130.81, 196.00, 261.63, 329.63),
        (98.00, 146.83, 196.00, 246.94),
    ]
    samples = []
    for i in range(total):
        t = i / sr
        chord = chords[int(t // 8.0) % len(chords)]
        pulse = 0.86 + 0.14 * math.sin(2 * math.pi * 1.1 * t)
        val = sum(math.sin(2 * math.pi * f * t) * w for f, w in zip(chord, (0.42, 0.28, 0.18, 0.12)))
        fade_in = clamp(t / 1.2)
        fade_out = clamp((duration - t) / 2.0)
        samples.append(val * pulse * fade_in * fade_out * 0.12)
    write_wav(path, samples)


async def synth_voice(scene: dict, voice: str) -> Path:
    text = scene["voice"].strip()
    h = hashlib.md5(f"{voice}:{text}".encode("utf-8")).hexdigest()[:12]
    mp3_path = CACHE_DIR / f"{scene['id']}_{h}.mp3"
    wav_path = CACHE_DIR / f"{scene['id']}_{h}.wav"

    if not wav_path.exists():
        if not mp3_path.exists():
            ok = False
            for v in (voice, "ru-RU-SvetlanaNeural"):
                for attempt in range(3):
                    try:
                        comm = edge_tts.Communicate(text, v, rate="+8%")
                        await comm.save(str(mp3_path))
                        ok = True
                        break
                    except Exception as exc:
                        print(f"[!] Ошибка TTS ({v}, попытка {attempt + 1}): {exc}")
                        await asyncio.sleep(1.5)
                if ok:
                    break
            if not ok:
                raise RuntimeError(f"Не удалось синтезировать озвучку для {scene['id']}")

        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), "-ar", str(SAMPLE_RATE), "-ac", "1", str(wav_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    return wav_path


def mix_scene_audio(sid: str, vo_wav: Path, dur: float) -> Path:
    out_wav = OUT_DIR / f"{sid}_audio.wav"
    if sid == "win_sc1_hook":
        d_ms = int(dur * 0.72 * 1000)
        flt = f"[1:a]adelay={d_ms}|{d_ms}[s1];[0:a][s1]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_lost.wav")]
    elif sid == "win_sc2_pain":
        p1 = int(dur * 0.15 * 1000)
        p2 = int(dur * 0.30 * 1000)
        p3 = int(dur * 0.45 * 1000)
        flt = (
            f"[1:a]asplit=3[s1][s2][s3];"
            f"[s1]adelay={p1}|{p1}[p1];[s2]adelay={p2}|{p2}[p2];[s3]adelay={p3}|{p3}[p3];"
            f"[0:a][p1][p2][p3]amix=inputs=4:normalize=0,apad,atrim=0:{dur}"
        )
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_pop.wav")]
    elif sid == "win_sc3_solution":
        flt = f"[0:a][1:a]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_whoosh.wav")]
    elif sid == "win_sc4_demo":
        p1 = int(dur * 0.32 * 1000)
        tg = int(dur * 0.68 * 1000)
        flt = (
            f"[1:a]adelay={p1}|{p1}[p1];[2:a]adelay={tg}|{tg}[tg];"
            f"[0:a][p1][tg]amix=inputs=3:normalize=0,apad,atrim=0:{dur}"
        )
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_pop.wav"), "-i", str(OUT_DIR / "sfx_tg.wav")]
    elif sid == "win_sc5_roi":
        c1 = int(dur * 0.58 * 1000)
        flt = f"[1:a]adelay={c1}|{c1}[c1];[0:a][c1]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_cash.wav")]
    else:
        flt = f"[0:a][1:a]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_whoosh.wav")]

    subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", flt, "-ar", str(SAMPLE_RATE), "-ac", "1", str(out_wav)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return out_wav


def render_scene_video(
    sid: str,
    dur: float,
    start_offset: float,
    total_dur: float,
    fps: int,
    crf: int,
    cfg: dict,
) -> Path:
    out_mp4 = OUT_DIR / f"{sid}_video.mp4"
    renderer = RENDERERS[sid]
    total_frames = int(round(dur * fps))

    print(f"[*] Рендер {sid}: {dur:.2f} с ({total_frames} кадров @ {fps} fps)...")
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{W}x{H}",
        "-pix_fmt", "rgb24",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "faster",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for i in range(total_frames):
            t = i / fps
            gr = (start_offset + t) / max(0.01, total_dur)
            frame = renderer(t, dur, gr, cfg)
            proc.stdin.write(frame.tobytes())
    except (BrokenPipeError, OSError):
        pass
    finally:
        proc.stdin.close()
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace")[-400:] if stderr else ""
        raise RuntimeError(f"FFmpeg вернул код {proc.returncode} на сцене {sid}:\n{err_msg}")
    return out_mp4


async def build_promo(args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    QA_DIR.mkdir(parents=True, exist_ok=True)

    cfg = {"bot": args.bot, "site": args.site}

    print("[1/5] Синтез звуковых эффектов...")
    build_sfx_library(OUT_DIR)

    voice_dir = Path(args.voice_dir) if args.voice_dir else CUSTOM_VOICE_DIR
    use_custom = voice_dir.exists() and all((voice_dir / f"{sc['id']}.wav").exists() for sc in SCENES)

    vo_paths: List[Path] = []
    durs: List[float] = []

    if use_custom:
        print(f"[2/5] Используется студийная озвучка ElevenLabs из: {voice_dir}")
        for sc in SCENES:
            vp = voice_dir / f"{sc['id']}.wav"
            d_raw = wav_duration(vp)
            d_final = round(d_raw, 2)
            vo_paths.append(vp)
            durs.append(d_final)
            print(f"   • {sc['id']}: {d_final:.2f} с (ElevenLabs мастер)")
    else:
        print(f"[2/5] Синтез озвучки edge-tts ({args.voice}, rate=+8%)...")
        for sc in SCENES:
            vp = await synth_voice(sc, args.voice)
            d_raw = wav_duration(vp)
            d_final = round(max(4.5, d_raw + 0.65), 2)
            vo_paths.append(vp)
            durs.append(d_final)
            print(f"   • {sc['id']}: {d_final:.2f} с (речь {d_raw:.2f} с)")

    total_dur = sum(durs) - XFADE_SEC * (len(durs) - 1)
    print(f"[+] Итоговый хронометраж ролика: {total_dur:.2f} с")

    print("[3/5] Генерация ambient-подложки и рендер 6 сцен...")
    bgm_path = OUT_DIR / "ambient_pad.wav"
    build_ambient_pad(bgm_path, total_dur + 1.5)

    scene_videos: List[Path] = []
    scene_audios: List[Path] = []
    offset = 0.0
    for sc, vp, d_sec in zip(SCENES, vo_paths, durs):
        v_mp4 = render_scene_video(sc["id"], d_sec, offset, total_dur, args.fps, args.crf, cfg)
        a_wav = mix_scene_audio(sc["id"], vp, d_sec)
        scene_videos.append(v_mp4)
        scene_audios.append(a_wav)
        offset += d_sec - XFADE_SEC

    print("[4/5] Склейка с плавными xfade-переходами и мастеринг loudnorm (-14 LUFS)...")
    inputs: List[str] = []
    for v in scene_videos:
        inputs += ["-i", str(v)]
    for a in scene_audios:
        inputs += ["-i", str(a)]
    inputs += ["-i", str(bgm_path)]

    n = len(SCENES)
    bg_idx = 2 * n

    v_filters: List[str] = []
    prev_v = "[0:v]"
    acc = durs[0]
    for i in range(1, n):
        xf_offset = round(acc - XFADE_SEC, 3)
        out_lbl = f"[v{i}]" if i < n - 1 else "[vout]"
        v_filters.append(f"{prev_v}[{i}:v]xfade=transition=fade:duration={XFADE_SEC}:offset={xf_offset}{out_lbl}")
        prev_v = out_lbl
        acc += durs[i] - XFADE_SEC

    a_filters: List[str] = []
    prev_a = f"[{n}:a]"
    for i in range(1, n):
        out_lbl = f"[a{i}]" if i < n - 1 else "[avoice]"
        a_filters.append(f"{prev_a}[{n + i}:a]acrossfade=d={XFADE_SEC}:c1=tri:c2=tri{out_lbl}")
        prev_a = out_lbl

    a_filters.append(
        f"[avoice][{bg_idx}:a]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )

    filter_complex = ";".join(v_filters + a_filters)
    final_mp4 = OUT_DIR / "zayavka24_windows_promo_16x9.mp4"

    subprocess.run(
        [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level:v", "4.1",
            "-preset", "medium",
            "-crf", str(args.crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", str(SAMPLE_RATE),
            "-movflags", "+faststart",
            str(final_mp4),
        ],
        check=True,
    )

    print("[5/5] Генерация постера poster_windows.webp и контрольных QA-кадров...")
    poster_time = sum(durs[:3]) - 3 * XFADE_SEC + durs[3] * 0.70
    poster_path = OUT_DIR / "poster_windows.webp"
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", f"{poster_time:.2f}",
            "-i", str(final_mp4), "-vframes", "1", "-c:v", "libwebp", "-quality", "90",
            str(poster_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )

    t_cursor = 0.0
    for sc, d_sec in zip(SCENES, durs):
        shot_t = t_cursor + d_sec * 0.65
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{shot_t:.2f}",
                "-i", str(final_mp4), "-vframes", "1",
                str(QA_DIR / f"{sc['id']}.png"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        t_cursor += d_sec - XFADE_SEC

    size_mb = final_mp4.stat().st_size / (1024 * 1024)
    print(f"\nГОТОВО! Размер файла: {size_mb:.2f} МБ (ElevenLabs мастер)")
    print(f"Видео:  {final_mp4.resolve()}")
    print(f"Постер: {poster_path.resolve()}")
    print(f"QA:     {QA_DIR.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Генератор моушен-ролика Заявка 24/7 (Окна)")
    parser.add_argument("--fps", type=int, default=30, help="Кадров в секунду (30 или 60)")
    parser.add_argument("--crf", type=int, default=22, help="Качество H.264 (19 — сайт, 23 — лёгкий для TG/ВК)")
    parser.add_argument("--voice", type=str, default="ru-RU-DmitryNeural", help="Голос edge-tts (если нет ElevenLabs)")
    parser.add_argument("--voice-dir", type=str, default="", help="Директория с аудиофайлами сцен (.wav)")
    parser.add_argument("--bot", type=str, default="@Zayavka24AI_bot", help="Telegram-бот для CTA")
    parser.add_argument("--site", type=str, default="zayavka24.ru", help="Домен сайта для CTA")
    args = parser.parse_args()
    asyncio.run(build_promo(args))


if __name__ == "__main__":
    main()
