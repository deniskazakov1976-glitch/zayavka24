"""
Студийный генератор продающего моушен-ролика для «Заявка 24/7» (1920x1080 Full HD).

Особенности:
- Настоящий моушен: spring-анимации, дрейфующие градиентные сферы, техно-сетка,
  бегущие потоки данных, живой индикатор печати, растущие ROI-графики и блик на CTA.
- 0 битых эмодзи: все иконки рисуются процедурно как чёткая векторная графика.
- Плавные переходы между сценами (FFmpeg xfade + acrossfade).
- Озвучка edge-tts (DmitryNeural, rate=+8%) с локальным кэшированием,
  автосинхронизация субтитров по реальной длительности речи, ambient-подложка и loudnorm.
- Генерация постера poster.webp для лендинга.

Запуск:
    python scripts/render_motion_promo.py
    python scripts/render_motion_promo.py --fps 60 --bot @Zayavka24AI_bot --site zayavka24.ru
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

OUT_DIR = Path("video_motion_promo")
CACHE_DIR = OUT_DIR / "cache"
QA_DIR = OUT_DIR / "qa_frames"

# --- Цветовая палитра (Премиум Dark Auto-Tech) ---
BG_BASE = (6, 9, 16)
SURFACE = (13, 19, 32)
SURFACE_ELEV = (20, 29, 48)
BORDER = (38, 52, 78)
EMERALD = (0, 229, 153)
EMERALD_DARK = (10, 42, 34)
CYAN = (0, 210, 255)
BLUE = (59, 130, 246)
RED = (244, 63, 94)
RED_DARK = (42, 14, 24)
AMBER = (245, 158, 11)
WHITE = (248, 250, 252)
MUTED = (148, 163, 184)
FAINT = (94, 110, 136)


# --- Загрузка шрифтов (Windows + Linux) ---
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


F_HERO = load_font(76, bold=True)
F_H1 = load_font(54, bold=True)
F_H2 = load_font(38, bold=True)
F_H3 = load_font(30, bold=True)
F_BODY = load_font(26, bold=False)
F_BODY_B = load_font(26, bold=True)
F_SMALL = load_font(21, bold=False)
F_BADGE = load_font(22, bold=True)
F_MONO_XL = load_font(86, bold=True, mono=True)
F_MONO_LG = load_font(52, bold=True, mono=True)
F_MONO_MD = load_font(34, bold=True, mono=True)
F_SUB = load_font(34, bold=True)


# --- Математика анимаций (Easing) ---
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


# --- Предварительный рендер фоновых спрайтов (быстро на CPU) ---
class VisualAssets:
    def __init__(self) -> None:
        self.orb_emerald = self._make_orb((0, 229, 153), alpha=58, size=980)
        self.orb_cyan = self._make_orb((0, 210, 255), alpha=48, size=880)
        self.orb_red = self._make_orb((244, 63, 94), alpha=62, size=980)
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
        col = (255, 255, 255, 11)
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
    if kind == "bolt":
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
    elif kind == "phone":
        d.rounded_rectangle([cx - int(r * 0.55), cy - r, cx + int(r * 0.55), cy + r], radius=6, outline=color, width=w)
        d.line([(cx - int(r * 0.18), cy + int(r * 0.65)), (cx + int(r * 0.18), cy + int(r * 0.65))], fill=color, width=w)


# --- Вспомогательные функции отрисовки ---
def draw_card(
    d: ImageDraw.ImageDraw,
    box: List[int],
    fill=SURFACE,
    outline=BORDER,
    width: int = 2,
    radius: int = 20,
) -> None:
    x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    if x2 <= x1 + 4 or y2 <= y1 + 4:
        return
    d.rounded_rectangle([x1, y1, x2, y2], radius=min(radius, (x2 - x1) // 2, (y2 - y1) // 2), fill=fill, outline=outline, width=width)


def text_size(font: ImageFont.FreeTypeFont, text: str) -> Tuple[int, int]:
    bb = font.getbbox(text)
    return (bb[2] - bb[0], bb[3] - bb[1])


def draw_centered(
    d: ImageDraw.ImageDraw,
    text: str,
    cx: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    fill=WHITE,
) -> None:
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
# СЦЕНАРИЙ И 6 МОУШЕН-СЦЕН
# ==============================================================================
SCENES = [
    {
        "id": "sc1_hook",
        "voice": (
            "Двадцать три сорок два. Автосервис закрыт, мастера спят. "
            "Клиент пишет на Авито по оклейке — ждёт три минуты и уходит к конкуренту. "
            "Минус сорок пять тысяч рублей."
        ),
        "subs": [
            (0.0, 0.45, "23:42 • Сервис закрыт, клиент пишет на Авито"),
            (0.45, 0.78, "Ждёт ответа 3 минуты и уходит к конкуренту"),
            (0.78, 1.0, "Минус 45 000 ₽ упущенной кассы с одной заявки"),
        ],
    },
    {
        "id": "sc2_pain",
        "voice": (
            "До тридцати пяти процентов заявок приходят вечером и в выходные. "
            "Пока вы в боксе или отдыхаете, мимо кассы утекает до двухсот тысяч рублей каждый месяц."
        ),
        "subs": [
            (0.0, 0.48, "До 35% обращений приходят вечером и в выходные"),
            (0.48, 1.0, "Мимо кассы утекает до 200 000 ₽ каждый месяц"),
        ],
    },
    {
        "id": "sc3_solution",
        "voice": (
            "Заявка двадцать четыре на семь — ваш ИИ-старший мастер. "
            "Он отвечает за пятнадцать секунд в Авито, Ватсапе и Телеграме, "
            "строго держит ваш прайс и не выдумывает скидки."
        ),
        "subs": [
            (0.0, 0.34, "«Заявка 24/7» — ваш ИИ-старший мастер"),
            (0.34, 0.70, "Ответ за 15 секунд в Авито, WhatsApp и Telegram"),
            (0.70, 1.0, "Строго по вашему прайсу — без выдуманных скидок"),
        ],
    },
    {
        "id": "sc4_demo",
        "voice": (
            "Клиент пишет ночью — бот уточняет автомобиль, называет вилку цен из прайса, "
            "мягко берёт номер телефона и за секунду отправляет готовую заявку вам в Телеграм и ЦРМ."
        ),
        "subs": [
            (0.0, 0.32, "Клиент пишет ночью на Авито -> ИИ включается за 15 сек"),
            (0.32, 0.68, "Уточняет авто, называет вилку цен и забирает телефон"),
            (0.68, 1.0, "Готовый лид мгновенно падает в Telegram и CRM"),
        ],
    },
    {
        "id": "sc5_roi",
        "voice": (
            "Настройка под ваш прайс занимает десять минут. "
            "Подписка стоит двенадцать девятьсот в месяц и полностью окупается уже с первой спасённой машины."
        ),
        "subs": [
            (0.0, 0.38, "Подключение под ваш прайс-лист за 10 минут"),
            (0.38, 1.0, "Подписка 12 900 ₽/мес окупается с 1-й спасённой машины"),
        ],
    },
    {
        "id": "sc6_cta",
        "voice": (
            "Запустите бесплатный тест-драйв на три дня прямо сейчас — "
            "и посмотрите, сколько клиентов бот спасёт уже этой ночью."
        ),
        "subs": [
            (0.0, 0.52, "Бесплатный тест-драйв на 3 дня без привязки карты"),
            (0.52, 1.0, "Начните спасать ночные заявки уже сегодня"),
        ],
    },
]


def render_sc1(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="red")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, RED)

    draw_pill(d, "23:42  •  АВТОСЕРВИС ЗАКРЫТ  •  МАСТЕРА СПЯТ", W // 2, 42, bg=RED_DARK, border=RED, text_col=(252, 165, 165), icon_kind="clock")

    # Левая карточка: Чат Авито
    e_left = ease_out_cubic(prog(t, 0.0, 0.55))
    lx_off = int((1.0 - e_left) * -70)
    lx1, ly1, lx2, ly2 = 80 + lx_off, 126, 980 + lx_off, 916
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    draw_card(d, [lx1, ly1, lx2, ly1 + 78], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    d.ellipse([lx1 + 28, ly1 + 26, lx1 + 52, ly1 + 50], fill=(0, 170, 255))
    d.text((lx1 + 68, ly1 + 22), "Авито Сообщения  •  Детейлинг и оклейка", font=F_H3, fill=WHITE)

    # Сообщение клиента выезжает
    e_msg = ease_out_back(prog(t, 0.3, 0.45))
    my = ly1 + 114 + int((1.0 - e_msg) * 35)
    draw_card(d, [lx1 + 36, my, lx2 - 36, my + 178], fill=(22, 34, 58), outline=(58, 80, 118), width=2, radius=18)
    d.text((lx1 + 64, my + 22), "Автовладелец  •  23:42", font=F_BODY_B, fill=CYAN)
    draw_wrapped(
        d,
        "Здравствуйте! Сколько стоит оклейка зон риска на Geely Monjaro плёнкой Llumar? Хочу записаться на этой неделе.",
        lx1 + 64,
        my + 66,
        740,
        F_BODY,
        WHITE,
    )

    # Бегущий таймер ожидания
    p_wait = clamp(prog(t, 0.8, dur * 0.48))
    secs = int(p_wait * 198)
    ty = ly1 + 324
    pulse = 0.5 + 0.5 * math.sin(t * 9.0)
    t_border = mix_rgb((140, 35, 55), RED, pulse)
    draw_card(d, [lx1 + 36, ty, lx2 - 36, ty + 168], fill=RED_DARK, outline=t_border, width=2, radius=18)
    draw_icon(d, "clock", lx1 + 82, ty + 54, 22, RED)
    d.text((lx1 + 122, ty + 32), f"Клиент ждёт ответ:  0{secs // 60}:{secs % 60:02d}", font=F_H2, fill=(254, 202, 202))
    # Прогресс-бар ожидания
    bar_x1, bar_y1, bar_x2, bar_y2 = lx1 + 64, ty + 104, lx2 - 64, ty + 124
    draw_card(d, [bar_x1, bar_y1, bar_x2, bar_y2], fill=(28, 12, 20), outline=RED_DARK, width=1, radius=10)
    fill_w = max(14, int((bar_x2 - bar_x1) * p_wait))
    draw_card(d, [bar_x1, bar_y1, bar_x1 + fill_w, bar_y2], fill=RED, outline=RED, width=1, radius=10)
    d.text((lx1 + 64, ty + 134), "Никто не отвечает — мастера заняты или уже спят", font=F_SMALL, fill=MUTED)

    # Клиент ушёл к конкуренту
    e_left_chat = ease_out_back(prog(t, dur * 0.54, 0.42))
    if e_left_chat > 0.01:
        ly_alert = ly1 + 526 + int((1.0 - e_left_chat) * 40)
        draw_card(d, [lx1 + 36, ly_alert, lx2 - 36, ly_alert + 104], fill=(55, 16, 28), outline=RED, width=3, radius=18)
        draw_icon(d, "cross", lx1 + 82, ly_alert + 52, 20, RED)
        d.text((lx1 + 122, ly_alert + 34), "Клиент закрыл чат и ушёл к конкуренту", font=F_H3, fill=(254, 202, 202))

    # Правая карточка: -45 000 ₽
    e_right = ease_out_cubic(prog(t, 0.15, 0.55))
    rx_off = int((1.0 - e_right) * 70)
    rx1, ry1, rx2, ry2 = 1020 + rx_off, 126, W - 80 + rx_off, 916
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(20, 12, 22), outline=RED, width=3, radius=24)
    d.text((rx1 + 48, ry1 + 52), "УПУЩЕННАЯ ВЫРУЧКА С 1 ЗАЯВКИ", font=F_BADGE, fill=(252, 165, 165))

    p_loss = ease_out_cubic(prog(t, dur * 0.56, 0.65))
    loss_val = int(45000 * p_loss)
    d.text((rx1 + 48, ry1 + 108), f"-{loss_val:,} ₽".replace(",", " "), font=F_MONO_XL, fill=RED)

    draw_wrapped(
        d,
        "Автовладельцы пишут сразу в 3–4 сервиса на Авито.\nЗаказ забирает тот, кто ответил первым за 1–2 минуты.",
        rx1 + 48,
        ry1 + 245,
        720,
        F_H3,
        WHITE,
        line_gap=14,
    )

    facts = [
        ("78%", "клиентов не ждут ответа дольше 5 минут"),
        ("23:00 – 08:00", "самые горячие и дорогие чеки без конкуренции"),
        ("0 ₽ в кассу", "если мастер прочитал сообщение только утром"),
    ]
    for i, (k, desc) in enumerate(facts):
        ef = ease_out_cubic(prog(t, 0.9 + i * 0.45, 0.45))
        if ef <= 0.01:
            continue
        fy = ry1 + 430 + i * 112 + int((1.0 - ef) * 28)
        draw_card(d, [rx1 + 48, fy, rx2 - 48, fy + 92], fill=(32, 16, 28), outline=(76, 30, 44), width=1, radius=14)
        d.text((rx1 + 74, fy + 14), k, font=F_H3, fill=RED)
        d.text((rx1 + 74, fy + 54), desc, font=F_SMALL, fill=MUTED)

    draw_subtitle(d, active_subtitle(SCENES[0]["subs"], t, dur))
    return img


def render_sc2(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="red")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, RED)

    draw_pill(d, "ЕЖЕМЕСЯЧНАЯ УТЕЧКА ЗАЯВОК В АВТОБИЗНЕСЕ", W // 2, 42, bg=RED_DARK, border=RED, text_col=(252, 165, 165), icon_kind="cross")

    # Слева: каскад пропущенных машин
    lx1, ly1, lx2, ly2 = 80, 126, 980, 916
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    draw_card(d, [lx1, ly1, lx2, ly1 + 74], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    d.text((lx1 + 36, ly1 + 20), "Пропущенные чаты (вечер и выходные)", font=F_H3, fill=WHITE)

    missed = [
        ("BMW X5  •  23:55", "Локальный окрас двери и крыла. Какие сроки?", "-38 000 ₽"),
        ("Toyota Camry  •  00:30", "Покраска переднего бампера в цвет. Готов завтра.", "-18 000 ₽"),
        ("Kia K5  •  01:15", "Оклейка передней части полиуретаном Llumar?", "-54 000 ₽"),
        ("Geely Monjaro  •  02:40", "Керамика в 2 слоя + бронь фар. Сколько под ключ?", "-75 000 ₽"),
    ]
    for i, (head, q, loss) in enumerate(missed):
        em = ease_out_back(prog(t, 0.25 + i * 0.75, 0.45))
        if em <= 0.01:
            continue
        x_shift = int((1.0 - em) * -60)
        cy1 = ly1 + 102 + i * 165
        draw_card(d, [lx1 + 32 + x_shift, cy1, lx2 - 32 + x_shift, cy1 + 142], fill=(24, 17, 30), outline=(120, 38, 56), width=2, radius=16)
        d.text((lx1 + 58 + x_shift, cy1 + 18), head, font=F_BODY_B, fill=CYAN)
        d.text((lx2 - 235 + x_shift, cy1 + 16), loss, font=F_H3, fill=RED)
        draw_wrapped(d, q, lx1 + 58 + x_shift, cy1 + 64, 760, F_BODY, WHITE)
        d.text((lx1 + 58 + x_shift, cy1 + 106), "Без ответа > 15 минут  •  заявка потеряна", font=F_SMALL, fill=(252, 165, 165))

    # Справа: бегущий счётчик -185 000 ₽
    rx1, ry1, rx2, ry2 = 1020, 126, W - 80, 916
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(20, 12, 22), outline=RED, width=3, radius=24)
    d.text((rx1 + 48, ry1 + 52), "МИМО КАССЫ КАЖДЫЙ МЕСЯЦ", font=F_BADGE, fill=(252, 165, 165))

    p_cnt = ease_out_cubic(prog(t, 0.4, 3.6))
    val = int(185000 * p_cnt)
    d.text((rx1 + 48, ry1 + 108), f"-{val:,} ₽".replace(",", " "), font=F_MONO_XL, fill=RED)

    # Анимированный индикатор 35%
    d.text((rx1 + 48, ry1 + 250), "Доля потерянных вечерних и ночных заявок:", font=F_BODY_B, fill=WHITE)
    p_bar = ease_out_cubic(prog(t, 0.6, 2.2))
    pct = int(35 * p_bar)
    d.text((rx2 - 150, ry1 + 244), f"{pct}%", font=F_MONO_MD, fill=RED)
    bx1, by1, bx2, by2 = rx1 + 48, ry1 + 302, rx2 - 48, ry1 + 332
    draw_card(d, [bx1, by1, bx2, by2], fill=(35, 16, 26), outline=(80, 30, 44), width=1, radius=14)
    draw_card(d, [bx1, by1, bx1 + max(18, int((bx2 - bx1) * 0.35 * p_bar)), by2], fill=RED, outline=RED, width=1, radius=14)

    draw_wrapped(
        d,
        "Вы уже заплатили Авито за рекламу и продвижение.\nНо без мгновенного ответа этот бюджет работает на конкурентов.",
        rx1 + 48,
        ry1 + 390,
        720,
        F_H3,
        MUTED,
        line_gap=14,
    )

    draw_card(d, [rx1 + 48, ry1 + 590, rx2 - 48, ry1 + 730], fill=(32, 16, 28), outline=RED, width=2, radius=18)
    d.text((rx1 + 76, ry1 + 616), "Средний автосервис (8 заявок/день):", font=F_BODY_B, fill=(252, 165, 165))
    d.text((rx1 + 76, ry1 + 662), "84 потерянных диалога/мес  =  8–10 ушедших машин", font=F_BODY, fill=WHITE)

    draw_subtitle(d, active_subtitle(SCENES[1]["subs"], t, dur))
    return img


def render_sc3(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="emerald")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, EMERALD)

    draw_pill(d, "РЕШЕНИЕ  •  ИИ-СТАРШИЙ МАСТЕР 24/7", W // 2, 42, bg=EMERALD_DARK, border=EMERALD, text_col=EMERALD, icon_kind="bolt")

    # Верхняя схема потоков: Каналы -> Ядро -> CRM
    hub_y = 300
    hub_cx = W // 2

    # Линии связей + бегущие пакеты данных
    left_nodes = [
        ("Авито Pro", "API v3", (0, 170, 255), 175),
        ("WhatsApp", "Green-API", (37, 211, 102), 300),
        ("Telegram", "Личный бот", (56, 189, 248), 425),
    ]
    right_nodes = [
        ("Telegram мастера", "Мгновенный пуш", EMERALD, 175),
        ("amoCRM", "Сделка + контакт", (76, 139, 245), 300),
        ("Битрикс24", "Автовыгрузка", CYAN, 425),
    ]

    for i, (title, sub, col, ny) in enumerate(left_nodes):
        # Линия в центр
        d.line([(470, ny), (755, hub_y)], fill=(38, 62, 74), width=3)
        # Бегущий импульс
        p_dot = (t * 0.9 + i * 0.33) % 1.0
        px = int(lerp(470, 755, p_dot))
        py = int(lerp(ny, hub_y, p_dot))
        d.ellipse([px - 7, py - 7, px + 7, py + 7], fill=col)

        en = ease_out_cubic(prog(t, 0.1 + i * 0.15, 0.45))
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

        en = ease_out_cubic(prog(t, 0.35 + i * 0.15, 0.45))
        ox = int((1.0 - en) * 50)
        draw_card(d, [1450 + ox, ny - 44, 1810 + ox, ny + 44], fill=SURFACE, outline=col, width=2, radius=16)
        d.ellipse([1476 + ox, ny - 10, 1496 + ox, ny + 10], fill=col)
        d.text((1514 + ox, ny - 26), title, font=F_H3, fill=WHITE)
        d.text((1514 + ox, ny + 10), sub, font=F_SMALL, fill=MUTED)

    # Центральное пульсирующее ядро «Заявка 24/7»
    pulse = 0.5 + 0.5 * math.sin(t * 4.5)
    core_border = mix_rgb((0, 175, 120), EMERALD, pulse)
    draw_card(d, [hub_cx - 210, hub_y - 125, hub_cx + 210, hub_y + 125], fill=EMERALD_DARK, outline=core_border, width=4, radius=28)
    draw_icon(d, "bolt", hub_cx, hub_y - 68, 28, EMERALD)
    draw_centered(d, "Заявка 24/7", hub_cx, hub_y - 20, F_H1, WHITE)
    draw_centered(d, "ИИ-диспетчер автобизнеса", hub_cx, hub_y + 46, F_BODY, EMERALD)
    draw_centered(d, "Ответ за 15 сек  •  24/7", hub_cx, hub_y + 82, F_SMALL, MUTED)

    # Нижний ряд: 3 карточки преимуществ
    cards = [
        ("bolt", "Ответ за 15 секунд", "Перехватывает клиента раньше конкурентов днём, ночью и в праздники.", EMERALD),
        ("shield", "Защита цен Guardrails", "Называет только цены из вашего прайса. Ложные скидки блокируются.", CYAN),
        ("pause", "Умная автопауза", "Написали клиенту сами — бот мгновенно замолкает и не мешает мастеру.", AMBER),
    ]
    cw = 560
    gap = 40
    start_x = (W - (cw * 3 + gap * 2)) // 2
    for i, (ico, title, desc, col) in enumerate(cards):
        ec = ease_out_back(prog(t, 0.7 + i * 0.45, 0.5))
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

    draw_pill(d, "КАК ЭТО РАБОТАЕТ  •  ЧАТ АВИТО  ->  TELEGRAM И CRM", W // 2, 42, bg=(10, 32, 46), border=CYAN, text_col=CYAN, icon_kind="send")

    # Слева: Живой диалог в Авито
    lx1, ly1, lx2, ly2 = 80, 122, 1000, 918
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    draw_card(d, [lx1, ly1, lx2, ly1 + 76], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    d.ellipse([lx1 + 28, ly1 + 26, lx1 + 48, ly1 + 46], fill=EMERALD)
    d.text((lx1 + 64, ly1 + 20), "Старший мастер «Заявка 24/7»  •  Онлайн", font=F_H3, fill=WHITE)

    # 1. Сообщение клиента
    e1 = ease_out_back(prog(t, 0.15, 0.45))
    if e1 > 0.01:
        y1 = ly1 + 104 + int((1.0 - e1) * 30)
        draw_card(d, [lx1 + 32, y1, lx2 - 110, y1 + 132], fill=(24, 38, 66), outline=(56, 86, 130), width=2, radius=18)
        d.text((lx1 + 56, y1 + 16), "Клиент  •  23:42", font=F_SMALL, fill=CYAN)
        draw_wrapped(d, "Сколько стоит оклейка зон риска на Geely Monjaro? Плёнка Llumar.", lx1 + 56, y1 + 48, 720, F_BODY, WHITE)

    # 2. Анимация «печатает...» (1.0s .. 2.5s)
    if 1.0 <= t < 2.5:
        ty = ly1 + 262
        draw_card(d, [lx1 + 140, ty, lx1 + 440, ty + 66], fill=EMERALD_DARK, outline=EMERALD, width=1, radius=18)
        for idx in range(3):
            bounce = int(abs(math.sin(t * 7.5 - idx * 0.7)) * 8)
            dx = lx1 + 174 + idx * 20
            dy = ty + 35 - bounce
            d.ellipse([dx - 5, dy - 5, dx + 5, dy + 5], fill=EMERALD)
        d.text((lx1 + 240, ty + 18), "ИИ отвечает...", font=F_SMALL, fill=EMERALD)

    # 3. Ответ ИИ-мастера (с 2.5s)
    e2 = ease_out_back(prog(t, 2.5, 0.45))
    if e2 > 0.01:
        y2 = ly1 + 260 + int((1.0 - e2) * 30)
        draw_card(d, [lx1 + 90, y2, lx2 - 32, y2 + 228], fill=EMERALD_DARK, outline=EMERALD, width=2, radius=18)
        d.text((lx1 + 118, y2 + 18), "ИИ-ассистент студии  •  23:42 (14 сек)", font=F_SMALL, fill=EMERALD)
        draw_wrapped(
            d,
            "Добрый вечер! Оклейка зон риска на Geely Monjaro полиуретаном Llumar — от 45 000 до 75 000 ₽. Срок 1 день, гарантия 5 лет.\nОставьте ваш номер — закрепим скидку 10% и подберём окно?",
            lx1 + 118,
            y2 + 52,
            740,
            F_BODY,
            WHITE,
            line_gap=8,
        )

    # 4. Клиент даёт телефон (с 5.6s)
    e3 = ease_out_back(prog(t, 5.6, 0.45))
    if e3 > 0.01:
        y3 = ly1 + 516 + int((1.0 - e3) * 30)
        draw_card(d, [lx1 + 32, y3, lx2 - 110, y3 + 118], fill=(24, 38, 66), outline=EMERALD, width=2, radius=18)
        d.text((lx1 + 56, y3 + 16), "Клиент  •  23:43", font=F_SMALL, fill=CYAN)
        d.text((lx1 + 56, y3 + 52), "Отлично, запишите на субботу: +7 (916) 555-32-11", font=F_BODY_B, fill=EMERALD)

    # 5. Бейдж перехвата лида внизу чата
    e4 = ease_out_cubic(prog(t, 6.4, 0.4))
    if e4 > 0.01:
        y4 = ly1 + 660 + int((1.0 - e4) * 20)
        draw_card(d, [lx1 + 32, y4, lx2 - 32, y4 + 78], fill=(12, 36, 30), outline=EMERALD, width=2, radius=14)
        draw_icon(d, "check", lx1 + 74, y4 + 39, 16, EMERALD)
        d.text((lx1 + 108, y4 + 23), "Номер распознан  ->  заявка отправлена мастеру за 1 сек", font=F_BODY_B, fill=EMERALD)

    # Справа: Telegram пуш + amoCRM сделка
    rx1, ry1, rx2, ry2 = 1040, 122, W - 80, 918
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(10, 18, 32), outline=BORDER, width=2, radius=24)
    draw_card(d, [rx1, ry1, rx2, ry1 + 76], fill=SURFACE_ELEV, outline=BORDER, width=1, radius=24)
    draw_icon(d, "bell", rx1 + 42, ry1 + 38, 16, EMERALD)
    d.text((rx1 + 74, ry1 + 20), "Телефон владельца и CRM", font=F_H3, fill=WHITE)

    if t < 6.7:
        draw_centered(d, "Ожидание квалификации клиента...", (rx1 + rx2) // 2, ry1 + 360, F_H3, FAINT)
    else:
        # Карточка Telegram выпадает с отскоком
        etg = ease_out_back(prog(t, 6.7, 0.5))
        tgy = ry1 + 108 + int((1.0 - etg) * -50)
        draw_card(d, [rx1 + 32, tgy, rx2 - 32, tgy + 370], fill=(10, 32, 30), outline=EMERALD, width=3, radius=20)
        draw_pill(d, "НОВАЯ ЗАЯВКА С АВИТО", rx1 + 200, tgy + 24, bg=EMERALD, border=EMERALD, text_col=(6, 9, 16))
        d.text((rx1 + 60, tgy + 88), "+7 (916) 555-32-11", font=F_MONO_LG, fill=WHITE)
        rows = [
            ("Автомобиль:", "Geely Monjaro"),
            ("Услуга:", "Оклейка зон риска (Llumar)"),
            ("Ориентир:", "45 000 – 75 000 ₽"),
            ("Запись:", "Суббота • контакт подтверждён"),
        ]
        for idx, (k, v) in enumerate(rows):
            ry_row = tgy + 168 + idx * 44
            d.text((rx1 + 60, ry_row), k, font=F_BODY, fill=MUTED)
            d.text((rx1 + 245, ry_row), v, font=F_BODY_B, fill=WHITE if idx != 2 else EMERALD)

        # Карточка CRM выезжает снизу
        ecrm = ease_out_back(prog(t, 7.6, 0.5))
        if ecrm > 0.01:
            cy = ry1 + 510 + int((1.0 - ecrm) * 50)
            draw_card(d, [rx1 + 32, cy, rx2 - 32, cy + 235], fill=(18, 28, 48), outline=CYAN, width=2, radius=20)
            draw_icon(d, "check", rx1 + 74, cy + 48, 18, CYAN)
            d.text((rx1 + 108, cy + 28), "amoCRM / Битрикс24", font=F_H2, fill=CYAN)
            d.text((rx1 + 60, cy + 92), "Сделка #1042 создана автоматически", font=F_BODY_B, fill=WHITE)
            d.text((rx1 + 60, cy + 136), "Этап: Запись на осмотр  •  Бюджет: 60 000 ₽", font=F_BODY, fill=EMERALD)
            d.text((rx1 + 60, cy + 180), "Прикреплена прямая ссылка на переписку Авито", font=F_SMALL, fill=MUTED)

    draw_subtitle(d, active_subtitle(SCENES[3]["subs"], t, dur))
    return img


def render_sc5(t: float, dur: float, gr: float, cfg: dict) -> Image.Image:
    img = ASSETS.make_bg(t, mood="emerald")
    d = ImageDraw.Draw(img)
    draw_scene_progress(d, gr, EMERALD)

    draw_pill(d, "ПРОЗРАЧНАЯ ЭКОНОМИКА  •  ОКУПАЕТСЯ С 1 МАШИНЫ", W // 2, 42, bg=EMERALD_DARK, border=EMERALD, text_col=EMERALD, icon_kind="check")

    # Левая панель: Анимированная диаграмма сравнения
    lx1, ly1, lx2, ly2 = 80, 126, 980, 916
    draw_card(d, [lx1, ly1, lx2, ly2], fill=SURFACE, outline=BORDER, width=2, radius=24)
    d.text((lx1 + 48, ly1 + 44), "Стоимость сервиса vs 1 спасённый клиент", font=F_H2, fill=WHITE)

    base_y = ly2 - 120
    # Ось
    d.line([(lx1 + 60, base_y), (lx2 - 60, base_y)], fill=BORDER, width=3)

    # Столбец 1: 12 900 ₽ (подписка)
    p1 = ease_out_cubic(prog(t, 0.3, 1.0))
    h1 = max(16, int(145 * p1))
    b1x1, b1x2 = lx1 + 140, lx1 + 380
    draw_card(d, [b1x1, base_y - h1, b1x2, base_y], fill=BLUE, outline=CYAN, width=2, radius=14)
    draw_centered(d, "12 900 ₽", (b1x1 + b1x2) // 2, base_y - h1 - 52, F_H2, CYAN)
    draw_centered(d, "Тариф «Старт»", (b1x1 + b1x2) // 2, base_y + 22, F_BODY_B, WHITE)
    draw_centered(d, "за целый месяц", (b1x1 + b1x2) // 2, base_y + 58, F_SMALL, MUTED)

    # Столбец 2: 48 000 ₽ (1 машина)
    p2 = ease_out_back(prog(t, 0.9, 1.2))
    h2 = max(16, int(480 * clamp(p2, 0.0, 1.05)))
    b2x1, b2x2 = lx1 + 490, lx1 + 750
    draw_card(d, [b2x1, base_y - h2, b2x2, base_y], fill=EMERALD, outline=(167, 243, 208), width=2, radius=16)
    draw_centered(d, "48 000 ₽", (b2x1 + b2x2) // 2, base_y - h2 - 56, F_H1, EMERALD)
    draw_centered(d, "1 спасённая машина", (b2x1 + b2x2) // 2, base_y + 22, F_BODY_B, WHITE)
    draw_centered(d, "оклейка / кузовной ремонт", (b2x1 + b2x2) // 2, base_y + 58, F_SMALL, MUTED)

    # Правая панель: Выгода и быстрый запуск
    rx1, ry1, rx2, ry2 = 1020, 126, W - 80, 916
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(10, 28, 26), outline=EMERALD, width=3, radius=24)

    d.text((rx1 + 48, ry1 + 48), "ЧИСТЫЙ ПЛЮС УЖЕ С 1-Й ЗАЯВКИ", font=F_BADGE, fill=EMERALD)
    p_profit = ease_out_cubic(prog(t, 1.3, 1.1))
    profit = int(35100 * p_profit)
    d.text((rx1 + 48, ry1 + 98), f"+{profit:,} ₽".replace(",", " "), font=F_MONO_XL, fill=WHITE)
    d.text((rx1 + 48, ry1 + 206), "а остальные 15–30 заявок за месяц — чистая прибыль", font=F_BODY, fill=EMERALD)

    # Пружинная печать
    es = ease_out_back(prog(t, 2.0, 0.5))
    if es > 0.01:
        sy = ry1 + 286 + int((1.0 - es) * 35)
        draw_card(d, [rx1 + 48, sy, rx2 - 48, sy + 110], fill=EMERALD, outline=WHITE, width=2, radius=18)
        draw_centered(d, "ОКУПАЕТСЯ С 1 КЛИЕНТА", (rx1 + rx2) // 2, sy + 30, F_H2, (6, 9, 16))

    steps = [
        ("01", "Скидываете ссылку на Авито или фото прайса"),
        ("02", "Подключаем бота за 10 минут без программистов"),
        ("03", "Получаете горячие телефоны клиентов 24/7"),
    ]
    for i, (num, txt) in enumerate(steps):
        ei = ease_out_cubic(prog(t, 0.6 + i * 0.45, 0.45))
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

    draw_pill(d, "СТАРТ ЗА 10 МИНУТ  •  БЕЗ ПРИВЯЗКИ КАРТЫ", W // 2, 42, bg=EMERALD_DARK, border=EMERALD, text_col=EMERALD, icon_kind="bolt")

    cx1, cy1, cx2, cy2 = 160, 122, W - 160, 916
    draw_card(d, [cx1, cy1, cx2, cy2], fill=SURFACE, outline=EMERALD, width=3, radius=30)

    # Логотип
    e_logo = ease_out_back(prog(t, 0.0, 0.5))
    ly = cy1 + 46 + int((1.0 - e_logo) * -25)
    draw_card(d, [W // 2 - 42, ly, W // 2 + 42, ly + 84], fill=EMERALD, outline=WHITE, width=2, radius=22)
    draw_icon(d, "bolt", W // 2, ly + 42, 26, (6, 9, 16))
    draw_centered(d, "Заявка 24/7", W // 2, ly + 102, F_HERO, WHITE)
    draw_centered(d, "ИИ-менеджер продаж для автосервисов, кузовного ремонта и детейлинга", W // 2, ly + 196, F_H3, CYAN)

    # Пульсирующая CTA-кнопка с бегущим бликом
    pulse = 0.5 + 0.5 * math.sin(t * 5.0)
    btn_fill = mix_rgb((0, 200, 134), (45, 245, 175), pulse)
    bw = 860
    bx1, by1, bx2, by2 = W // 2 - bw // 2, cy1 + 315, W // 2 + bw // 2, cy1 + 435
    draw_card(d, [bx1, by1, bx2, by2], fill=btn_fill, outline=WHITE, width=3, radius=24)

    # Световой блик, пробегающий по кнопке
    sheen_p = (t * 0.75) % 1.4
    if sheen_p <= 1.0:
        sx = int(lerp(bx1 + 20, bx2 - 90, sheen_p))
        d.rounded_rectangle([sx, by1 + 6, sx + 70, by2 - 6], radius=16, fill=(195, 255, 232))

    draw_centered(d, "ТЕСТ-ДРАЙВ 3 ДНЯ БЕСПЛАТНО", W // 2, by1 + 34, F_H1, (6, 9, 16))

    # 3 буллита гарантий
    guarantees = [
        "Настройка под ваш прайс за 10 минут",
        "Авито Pro + WhatsApp + Telegram + CRM",
        "Первые спасённые заявки уже этой ночью",
    ]
    gx_start = cx1 + 70
    col_w = (cx2 - cx1 - 140) // 3
    for i, gtxt in enumerate(guarantees):
        eg = ease_out_cubic(prog(t, 0.5 + i * 0.25, 0.45))
        if eg <= 0.01:
            continue
        gx = gx_start + i * col_w
        gy = cy1 + 478 + int((1.0 - eg) * 20)
        draw_icon(d, "check", gx + 20, gy + 18, 14, EMERALD)
        draw_wrapped(d, gtxt, gx + 48, gy, col_w - 64, F_BODY_B, WHITE, line_gap=6)

    # Нижняя плашка с контактами
    link_y = cy1 + 605
    draw_card(d, [cx1 + 80, link_y, cx2 - 80, link_y + 132], fill=SURFACE_ELEV, outline=CYAN, width=2, radius=20)
    draw_icon(d, "send", cx1 + 135, link_y + 66, 24, EMERALD)
    d.text((cx1 + 182, link_y + 22), f"Демо-бот в Telegram:   {cfg['bot']}", font=F_H2, fill=EMERALD)
    d.text((cx1 + 182, link_y + 76), f"Сайт и калькулятор:   {cfg['site']}", font=F_BODY, fill=CYAN)

    draw_subtitle(d, active_subtitle(SCENES[5]["subs"], t, dur))
    return img


RENDERERS: dict[str, Callable] = {
    "sc1_hook": render_sc1,
    "sc2_pain": render_sc2,
    "sc3_solution": render_sc3,
    "sc4_demo": render_sc4,
    "sc5_roi": render_sc5,
    "sc6_cta": render_sc6,
}


# ==============================================================================
# АУДИО-ИНЖИНИРИНГ (SFX, Ambient-подложка, edge-tts)
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

    # 1. Мягкий pop уведомления
    pop = [math.sin(2 * math.pi * (620 - 180 * (i / (0.14 * sr))) * (i / sr)) * math.exp(-(i / sr) * 28) * 0.32 for i in range(int(0.14 * sr))]
    write_wav(out_dir / "sfx_pop.wav", pop)

    # 2. Мягкий whoosh перехода
    dur_w = 0.42
    whoosh = [
        math.sin(2 * math.pi * (160 + 340 * math.sin(math.pi * (i / (dur_w * sr)))) * (i / sr))
        * (math.sin(math.pi * (i / (dur_w * sr))) ** 2) * 0.24
        for i in range(int(dur_w * sr))
    ]
    write_wav(out_dir / "sfx_whoosh.wav", whoosh)

    # 3. Тревожный низкий тон потери лида
    dur_l = 0.50
    lost = [
        (math.sin(2 * math.pi * (240 - 110 * (i / (dur_l * sr))) * (i / sr))
         + 0.35 * math.sin(2 * math.pi * (360 - 165 * (i / (dur_l * sr))) * (i / sr)))
        * math.exp(-(i / sr) * 5.5) * 0.28
        for i in range(int(dur_l * sr))
    ]
    write_wav(out_dir / "sfx_lost.wav", lost)

    # 4. Двухтоновый динь Telegram
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

    # 5. Аккорд окупаемости / успеха
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
    """Мягкая технологичная ambient-подложка с плавным fade-in/fade-out."""
    sr = SAMPLE_RATE
    total = int(duration * sr)
    chords = [
        (110.00, 164.81, 220.00, 261.63),  # Am7
        (87.31, 130.81, 174.61, 220.00),   # Fmaj7
        (130.81, 196.00, 261.63, 329.63),  # Cmaj7
        (98.00, 146.83, 196.00, 246.94),   # G6
    ]
    samples = []
    for i in range(total):
        t = i / sr
        chord = chords[int(t // 8.0) % len(chords)]
        pulse = 0.86 + 0.14 * math.sin(2 * math.pi * 1.1 * t)
        val = sum(math.sin(2 * math.pi * f * t) * w for f, w in zip(chord, (0.42, 0.28, 0.18, 0.12)))
        fade_in = clamp(t / 1.2)
        fade_out = clamp((duration - t) / 2.0)
        samples.append(val * pulse * fade_in * fade_out * 0.13)
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
    if sid == "sc1_hook":
        d_ms = int(dur * 0.56 * 1000)
        flt = f"[1:a]adelay={d_ms}|{d_ms}[s1];[0:a][s1]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_lost.wav")]
    elif sid == "sc2_pain":
        # Исправлено: в FFmpeg [1:a] нельзя подключать трижды без asplit
        flt = (
            f"[1:a]asplit=3[s1][s2][s3];"
            f"[s1]adelay=300|300[p1];[s2]adelay=1050|1050[p2];[s3]adelay=1800|1800[p3];"
            f"[0:a][p1][p2][p3]amix=inputs=4:normalize=0,apad,atrim=0:{dur}"
        )
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_pop.wav")]
    elif sid == "sc3_solution":
        flt = f"[0:a][1:a]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_whoosh.wav")]
    elif sid == "sc4_demo":
        flt = (
            f"[1:a]adelay=2500|2500[p1];[2:a]adelay=6700|6700[tg];"
            f"[0:a][p1][tg]amix=inputs=3:normalize=0,apad,atrim=0:{dur}"
        )
        inputs = ["-i", str(vo_wav), "-i", str(OUT_DIR / "sfx_pop.wav"), "-i", str(OUT_DIR / "sfx_tg.wav")]
    elif sid == "sc5_roi":
        flt = f"[1:a]adelay=2000|2000[c1];[0:a][c1]amix=inputs=2:normalize=0,apad,atrim=0:{dur}"
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
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert proc.stdin is not None
    for i in range(total_frames):
        t = i / fps
        gr = (start_offset + t) / max(0.01, total_dur)
        frame = renderer(t, dur, gr, cfg)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg вернул код {proc.returncode} на сцене {sid}")
    return out_mp4


async def build_promo(args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    QA_DIR.mkdir(parents=True, exist_ok=True)

    cfg = {"bot": args.bot, "site": args.site}

    print("[1/5] Синтез звуковых эффектов...")
    build_sfx_library(OUT_DIR)

    print(f"[2/5] Синтез озвучки ({args.voice}, rate=+8%)...")
    vo_paths: List[Path] = []
    durs: List[float] = []
    for sc in SCENES:
        vp = await synth_voice(sc, args.voice)
        d_raw = wav_duration(vp)
        # Небольшой запас 0.65с на дыхание и xfade-переход
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
        v_mp4 = render_scene_video(sc["id"], d_sec, offset, total_dur, args.fps, cfg)
        a_wav = mix_scene_audio(sc["id"], vp, d_sec)
        scene_videos.append(v_mp4)
        scene_audios.append(a_wav)
        offset += d_sec - XFADE_SEC

    print("[4/5] Склейка с плавными xfade-переходами и мастеринг loudnorm...")
    inputs: List[str] = []
    for v in scene_videos:
        inputs += ["-i", str(v)]
    for a in scene_audios:
        inputs += ["-i", str(a)]
    inputs += ["-i", str(bgm_path)]

    n = len(SCENES)
    bg_idx = 2 * n

    # Цепочка xfade для видео
    v_filters: List[str] = []
    prev_v = "[0:v]"
    acc = durs[0]
    for i in range(1, n):
        xf_offset = round(acc - XFADE_SEC, 3)
        out_lbl = f"[v{i}]" if i < n - 1 else "[vout]"
        v_filters.append(f"{prev_v}[{i}:v]xfade=transition=fade:duration={XFADE_SEC}:offset={xf_offset}{out_lbl}")
        prev_v = out_lbl
        acc += durs[i] - XFADE_SEC

    # Цепочка acrossfade для аудио + микс с ambient + loudnorm
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
    final_mp4 = OUT_DIR / "zayavka24_motion_promo_16x9.mp4"

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
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", str(SAMPLE_RATE),
            "-movflags", "+faststart",
            str(final_mp4),
        ],
        check=True,
    )

    print("[5/5] Генерация постера poster.webp и контрольных QA-кадров...")
    poster_time = sum(durs[:3]) - 3 * XFADE_SEC + 7.4
    poster_path = OUT_DIR / "poster.webp"
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

    print("\nГОТОВО!")
    print(f"Видео:  {final_mp4.resolve()}")
    print(f"Постер: {poster_path.resolve()}")
    print(f"QA:     {QA_DIR.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Генератор моушен-ролика Заявка 24/7")
    parser.add_argument("--fps", type=int, default=30, help="Кадров в секунду (30 или 60)")
    parser.add_argument("--voice", type=str, default="ru-RU-DmitryNeural", help="Голос edge-tts")
    parser.add_argument("--bot", type=str, default="@Zayavka24AI_bot", help="Telegram-бот для CTA")
    parser.add_argument("--site", type=str, default="zayavka24.ru", help="Домен сайта для CTA")
    args = parser.parse_args()
    asyncio.run(build_promo(args))


if __name__ == "__main__":
    main()
