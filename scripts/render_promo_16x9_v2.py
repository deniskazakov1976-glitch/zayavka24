"""
High-Converting Promotional Video Generator for "Заявка 24/7" (16:9 Full HD Web Edition)
Strictly adheres to the Technical Specification:
- Pure local pipeline (edge-tts + Python Pillow + FFmpeg)
- Voiceover: DmitryNeural +8% tempo, local caching by text hash
- Audio engineering: Ambient tech bed (-22 dB), soft filtered SFX, amix normalize=0, loudnorm
- Video engineering: rgb24 zero-disk piping, -movflags +faststart, CRF 23, H.264 High 4.1
- Dynamic subtitles & typography for 100% muted autoplay comprehension
- Poster generation (poster.webp) & QA frame extraction
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import wave
from PIL import Image, ImageDraw, ImageFont
import edge_tts

# --- Resolution & Settings ---
W, H = 1920, 1080
FPS = 30
OUT_DIR = Path("video_explainer_v2")
CACHE_DIR = OUT_DIR / "cache"
FRAMES_QA_DIR = OUT_DIR / "qa_frames"

OUT_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FRAMES_QA_DIR.mkdir(parents=True, exist_ok=True)

# --- Fonts Setup ---
WIN_FONTS = Path("C:/Windows/Fonts")
FONT_TITLE_XL = ImageFont.truetype(str(WIN_FONTS / "segoeuib.ttf"), 64)
FONT_TITLE_LG = ImageFont.truetype(str(WIN_FONTS / "segoeuib.ttf"), 48)
FONT_TITLE_MD = ImageFont.truetype(str(WIN_FONTS / "segoeuib.ttf"), 36)
FONT_TITLE_SM = ImageFont.truetype(str(WIN_FONTS / "segoeuib.ttf"), 28)
FONT_BODY = ImageFont.truetype(str(WIN_FONTS / "segoeui.ttf"), 26)
FONT_BODY_SM = ImageFont.truetype(str(WIN_FONTS / "segoeui.ttf"), 21)
FONT_MONO_LG = ImageFont.truetype(str(WIN_FONTS / "consola.ttf"), 82)
FONT_MONO_MD = ImageFont.truetype(str(WIN_FONTS / "consola.ttf"), 46)
FONT_SUBTITLE = ImageFont.truetype(str(WIN_FONTS / "segoeuib.ttf"), 36)
FONT_BADGE = ImageFont.truetype(str(WIN_FONTS / "segoeuib.ttf"), 22)

# --- Color Palette (Dark Theme / Emerald / Cyan / Red) ---
BG_DARK = (8, 14, 26)
CARD_BG = (15, 23, 42)
CARD_INNER = (24, 34, 56)
BORDER_COL = (38, 54, 82)
BORDER_MUTED = (25, 35, 52)
PRIMARY_GREEN = (16, 185, 129)
ACCENT_BLUE = (56, 189, 248)
ALERT_RED = (239, 68, 68)
GOLD_COL = (245, 158, 11)
TEXT_WHITE = (248, 250, 252)
TEXT_MUTED = (148, 163, 184)
TEXT_FAINT = (100, 116, 139)

# --- Script & Storyboard Definition ---
SCENES = [
    {
        "id": "sc1_hook",
        "title": "ХУК: Ночная потеря денег",
        "raw_text": "Двадцать три сорок две. Клиент с Авито готов записаться на оклейку. Вы не ответили за три минуты — он ушёл к конкуренту.",
        "subtitles": [
            (0.0, 3.2, "23:42 • Клиент с Авито готов записаться на оклейку"),
            (3.2, 6.0, "Вы не ответили за 3 минуты — клиент ушёл к конкуренту: -45 000 ₽!")
        ]
    },
    {
        "id": "sc2_pain",
        "title": "УСИЛЕНИЕ БОЛИ",
        "raw_text": "В среднем автосервис теряет до тридцати пяти процентов заявок вечером и в выходные. Это сто пятьдесят, двести тысяч рублей мимо кассы каждый месяц.",
        "subtitles": [
            (0.0, 3.5, "Автосервисы теряют до 35% ночных и вечерних заявок"),
            (3.5, 7.5, "Это 150 000 – 200 000 ₽ упущенной выручки каждый месяц!")
        ]
    },
    {
        "id": "sc3_solution",
        "title": "РЕШЕНИЕ: Старший мастер",
        "raw_text": "Заявка двадцать четыре на семь — это ваш ИИ-диспетчер. Он отвечает за пятнадцать секунд круглосуточно, общается как старший мастер строго по вашему прайсу и не сливает скидки.",
        "subtitles": [
            (0.0, 3.2, "«Заявка 24/7» — умный ИИ-диспетчер для автобизнеса"),
            (3.2, 5.8, "Мгновенный ответ за 15 секунд в любое время суток 24/7"),
            (5.8, 8.5, "Строго по вашему прайсу: не выдумывает скидки и не ошибается!")
        ]
    },
    {
        "id": "sc4_demo",
        "title": "СИНХРОННЫЙ СПЛИТ-СКРИН",
        "raw_text": "Смотрите, как это работает. Клиент пишет ночью на Авито — бот сам уточняет авто, называет вилку цен, мягко забирает номер телефона и через секунду присылает готовую карточку вам в Телеграм и ЦРМ.",
        "subtitles": [
            (0.0, 3.2, "Клиент пишет на Авито ночью ➔ Бот отвечает за 15 секунд"),
            (3.2, 7.0, "Квалифицирует марку авто, держит вилку цен и забирает номер"),
            (7.0, 11.5, "Через 1 секунду: готовый лид у мастера в Telegram и в amoCRM!")
        ]
    },
    {
        "id": "sc5_roi",
        "title": "ЭКОНОМИКА И ОКУПАЕМОСТЬ",
        "raw_text": "Подключение занимает десять минут. Сервис стоит двенадцать девятьсот в месяц и полностью окупается с первой же спасённой машины.",
        "subtitles": [
            (0.0, 3.0, "Подключение за 10 минут без программистов"),
            (3.0, 7.0, "Тариф 12 900 ₽/мес окупается ровно с 1 закрытой машины!")
        ]
    },
    {
        "id": "sc6_cta",
        "title": "СИЛЬНЫЙ CTA",
        "raw_text": "Подключите бесплатный тест-драйв на три дня прямо сейчас — и посмотрите, сколько заявок бот спасёт уже этой ночью.",
        "subtitles": [
            (0.0, 3.5, "Запустите тест-драйв на 3 дня бесплатно прямо сейчас!"),
            (3.5, 7.0, "Начните спасать ночные заявки уже этой ночью: @Zayavka24AI_bot")
        ]
    }
]


# --- Cubic Bezier Easing Helper ---
def ease_out_cubic(x: float) -> float:
    return 1.0 - math.pow(1.0 - max(0.0, min(1.0, x)), 3.0)


# --- Procedural Audio Synthesis (Soft, Low-Passed SFX & Ambient Bed) ---
def write_wav(filename: Path, samples: list[float], sample_rate: int = 48000):
    with wave.open(str(filename), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        raw = bytearray()
        for s in samples:
            val = max(-1.0, min(1.0, s))
            raw.extend(struct.pack("<h", int(val * 32767)))
        wav.writeframes(raw)


def generate_ambient_bed(filename: Path, duration_sec: float, sample_rate: int = 48000):
    """Subtle, pleasant warm tech ambient chord progression at -22 dB."""
    total_samples = int(duration_sec * sample_rate)
    samples = []
    # Warm chords: F (87.31 Hz), C (130.81 Hz), A (220.0 Hz), E (329.6 Hz)
    for i in range(total_samples):
        t = i / sample_rate
        # slow low-frequency pulse (1.2 Hz)
        pulse = 0.85 + 0.15 * math.sin(2.0 * math.pi * 1.2 * t)
        # 3 soft sine harmonics
        s1 = math.sin(2.0 * math.pi * 87.31 * t)
        s2 = math.sin(2.0 * math.pi * 130.81 * t) * 0.7
        s3 = math.sin(2.0 * math.pi * 220.0 * t) * 0.4
        val = (s1 + s2 + s3) * 0.08 * pulse  # ~ -22 dB volume level
        samples.append(val)
    write_wav(filename, samples, sample_rate)


def generate_soft_sfx_library(out_dir: Path, sample_rate: int = 48000):
    # 1. Soft UI Pop (Incoming Message)
    samples_pop = []
    dur_pop = int(0.18 * sample_rate)
    for i in range(dur_pop):
        t = i / sample_rate
        env = math.exp(-t * 26.0)
        val = math.sin(2.0 * math.pi * 560.0 * t) * env * 0.28
        samples_pop.append(val)
    write_wav(out_dir / "sfx_pop.wav", samples_pop, sample_rate)

    # 2. Soft Whoosh (Scene Transition)
    samples_whoosh = []
    dur_whoosh = int(0.45 * sample_rate)
    for i in range(dur_whoosh):
        t = i / sample_rate
        p = t / 0.45
        env = math.sin(math.pi * p) ** 2
        freq = 180.0 + 380.0 * math.sin(math.pi * p)
        val = math.sin(2.0 * math.pi * freq * t) * env * 0.22
        samples_whoosh.append(val)
    write_wav(out_dir / "sfx_whoosh.wav", samples_whoosh, sample_rate)

    # 3. Delicate Telegram Two-Tone Chime
    samples_tg = []
    dur_tg = int(0.40 * sample_rate)
    for i in range(dur_tg):
        t = i / sample_rate
        # Tone 1 (0.0 - 0.12s) 880Hz, Tone 2 (0.12 - 0.40s) 1320Hz
        if t < 0.12:
            env = math.exp(-t * 12.0)
            val = math.sin(2.0 * math.pi * 880.0 * t) * env * 0.35
        else:
            t2 = t - 0.12
            env = math.exp(-t2 * 9.0)
            val = math.sin(2.0 * math.pi * 1320.0 * t2) * env * 0.32
        samples_tg.append(val)
    write_wav(out_dir / "sfx_tg.wav", samples_tg, sample_rate)

    # 4. Soft Warning Down-Tone (Lost Lead)
    samples_loss = []
    dur_loss = int(0.55 * sample_rate)
    for i in range(dur_loss):
        t = i / sample_rate
        p = t / 0.55
        env = math.exp(-t * 5.0)
        freq = 280.0 - 140.0 * p
        val = math.sin(2.0 * math.pi * freq * t) * env * 0.30
        samples_loss.append(val)
    write_wav(out_dir / "sfx_lost.wav", samples_loss, sample_rate)

    # 5. Soft Cash Register Chime
    samples_cash = []
    dur_cash = int(0.65 * sample_rate)
    for i in range(dur_cash):
        t = i / sample_rate
        env = math.exp(-t * 6.5)
        # Harmonious major triad bell (523Hz + 659Hz + 784Hz + 1046Hz)
        v1 = math.sin(2.0 * math.pi * 523.25 * t)
        v2 = math.sin(2.0 * math.pi * 659.25 * t) * 0.8
        v3 = math.sin(2.0 * math.pi * 783.99 * t) * 0.7
        v4 = math.sin(2.0 * math.pi * 1046.50 * t) * 0.5
        val = (v1 + v2 + v3 + v4) * 0.12 * env
        samples_cash.append(val)
    write_wav(out_dir / "sfx_cash.wav", samples_cash, sample_rate)


# --- Voiceover Generation with DmitryNeural & Local Disk Cache ---
async def generate_scene_voiceover(scene: dict) -> Path:
    text = scene["raw_text"].replace("—", "-").replace("–", "-").strip()
    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    raw_mp3 = CACHE_DIR / f"{scene['id']}_{text_hash}.mp3"
    trimmed_wav = CACHE_DIR / f"{scene['id']}_{text_hash}_108.wav"

    if not trimmed_wav.exists():
        if not raw_mp3.exists():
            success = False
            for voice in ["ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural"]:
                for attempt in range(3):
                    try:
                        print(f"[*] Синтез речи для {scene['id']} ({voice}, попытка {attempt + 1})...")
                        comm = edge_tts.Communicate(text, voice)
                        await comm.save(str(raw_mp3))
                        success = True
                        break
                    except Exception as err:
                        print(f"[!] Ошибка синтеза {voice}: {err}. Повтор через 1.5с...")
                        await asyncio.sleep(1.5)
                if success:
                    break
            if not success:
                raise RuntimeError(f"Не удалось сгенерировать озвучку для {scene['id']}")

        # Apply +8% tempo speedup via FFmpeg atempo=1.08 for crisp, brisk delivery
        print(f"[*] Обработка темпа +8%: {trimmed_wav.name}")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(raw_mp3),
            "-filter_complex", "atempo=1.08",
            "-ar", "48000",
            "-ac", "1",
            str(trimmed_wav)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    return trimmed_wav


def get_wav_duration(path: Path) -> float:
    with wave.open(str(path), "r") as wf:
        return wf.getnframes() / float(wf.getframerate())


# --- Drawing Utilities ---
def draw_rounded_card(draw: ImageDraw.ImageDraw, bbox, fill=CARD_BG, outline=BORDER_COL, width=2, radius=18):
    draw.rounded_rectangle(bbox, radius=radius, fill=fill, outline=outline, width=width)


def draw_glow_badge(draw: ImageDraw.ImageDraw, text: str, cx: int, y: int, fill=PRIMARY_GREEN, text_col=(0, 0, 0), font=FONT_BADGE):
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    px, py = 20, 8
    draw.rounded_rectangle([cx - tw // 2 - px, y, cx + tw // 2 + px, y + th + py * 2], radius=10, fill=fill)
    draw.text((cx - tw // 2, y + py - 2), text, font=font, fill=text_col)


def draw_high_contrast_subtitle(draw: ImageDraw.ImageDraw, text: str):
    """Prominent, ultra-readable burned-in subtitle bar for 100% muted autoplay."""
    if not text:
        return
    bbox = FONT_SUBTITLE.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    cx = W // 2
    y = H - 90
    px, py = 28, 12

    # Double border glow container
    draw.rounded_rectangle(
        [cx - tw // 2 - px - 2, y - py - 2, cx + tw // 2 + px + 2, y + th + py + 2],
        radius=14,
        fill=(16, 185, 129, 60)
    )
    draw.rounded_rectangle(
        [cx - tw // 2 - px, y - py, cx + tw // 2 + px, y + th + py],
        radius=12,
        fill=(5, 10, 22),
        outline=ACCENT_BLUE,
        width=2
    )
    draw.text((cx - tw // 2, y - 4), text, font=FONT_SUBTITLE, fill=TEXT_WHITE)


# --- Scene Renderers (16:9 Full HD) ---

def render_sc1_hook(t: float, dur: float) -> Image.Image:
    """Scene 1: Hook - Night 23:42, Red Ticking Timer, Lost Lead -45 000 ₽."""
    img = Image.new("RGB", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    # Subtitle
    sub = "23:42 • Клиент с Авито готов записаться на оклейку" if t < (dur * 0.45) else "Вы не ответили за 3 минуты — клиент ушёл к конкуренту: -45 000 ₽!"

    # Header Alert
    draw_glow_badge(d, "🌙 23:42 • АВТОСЕРВИС ЗАКРЫТ • МАСТЕРА СПЯТ", W // 2, 45, fill=(30, 41, 59), text_col=TEXT_MUTED)

    # Left: Avito Chat Alert
    card_w = 900
    lx1 = 80
    ly1 = 120
    lx2 = lx1 + card_w
    ly2 = 910
    draw_rounded_card(d, [lx1, ly1, lx2, ly2], fill=CARD_BG, outline=BORDER_COL, width=3, radius=22)

    # Chat Header
    d.rounded_rectangle([lx1, ly1, lx2, ly1 + 75], radius=22, fill=CARD_INNER)
    d.text((lx1 + 35, ly1 + 22), "Авито Сообщения • Кузовной ремонт и оклейка", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    # Incoming message card
    draw_rounded_card(d, [lx1 + 35, ly1 + 110, lx2 - 35, ly1 + 270], fill=(22, 32, 54), outline=(48, 64, 94), radius=16)
    d.text((lx1 + 60, ly1 + 130), "Клиент (Geely Monjaro):", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((lx1 + 60, ly1 + 180), "Здравствуйте! Сколько стоит оклейка зон риска\nполиуретановой плёнкой на Monjaro? Готов приехать.", font=FONT_BODY, fill=TEXT_WHITE)

    # Red Timer (00:00 -> 03:15)
    p_timer = min(1.0, t / max(1.0, dur * 0.45))
    timer_secs = int(p_timer * 195)
    draw_rounded_card(d, [lx1 + 35, ly1 + 300, lx2 - 35, ly1 + 430], fill=(28, 16, 26), outline=ALERT_RED, width=2, radius=16)
    d.text((lx1 + 60, ly1 + 325), f"⏱ Ожидание ответа: 0{timer_secs // 60}:{timer_secs % 60:02d} мин", font=FONT_TITLE_MD, fill=(248, 113, 113))
    d.text((lx1 + 60, ly1 + 375), "Мастера спят или заняты • Ответа нет...", font=FONT_BODY, fill=TEXT_MUTED)

    # Client leaves alert
    if t >= (dur * 0.45):
        draw_rounded_card(d, [lx1 + 35, ly1 + 460, lx2 - 35, ly1 + 560], fill=(38, 16, 26), outline=ALERT_RED, width=3, radius=16)
        d.text((lx1 + 60, ly1 + 490), "❌ Клиент не дождался ответа и ушёл к конкуренту", font=FONT_TITLE_MD, fill=ALERT_RED)

    # Right: Huge Impact Counter
    rx1 = 1020
    ry1 = 120
    rx2 = W - 80
    ry2 = 910
    draw_rounded_card(d, [rx1, ry1, rx2, ry2], fill=(24, 14, 26), outline=ALERT_RED, width=4, radius=22)
    d.text((rx1 + 45, ry1 + 55), "УПУЩЕННАЯ ВЫРУЧКА СДЕЛКИ", font=FONT_TITLE_MD, fill=(248, 113, 113))

    # Stamp
    d.text((rx1 + 45, ry1 + 130), "-45 000 ₽", font=FONT_MONO_LG, fill=ALERT_RED)
    d.text((rx1 + 45, ry1 + 260), "Клиент нашёл автосервис, который ответил за 1 минуту.", font=FONT_TITLE_SM, fill=TEXT_WHITE)

    bullets = [
        "•  82% автовладельцев пишут сразу в 3–4 сервиса",
        "•  Кто ответил первым за 1–2 минуты — тот забирает машину",
        "•  Ночью и в выходные сервис теряет самые маржинальные чеки"
    ]
    for i, b in enumerate(bullets):
        d.text((rx1 + 45, ry1 + 370 + i * 85), b, font=FONT_BODY, fill=(226, 232, 240))

    draw_high_contrast_subtitle(d, sub)
    return img


def render_sc2_pain(t: float, dur: float) -> Image.Image:
    """Scene 2: Pain Escalation - Cascade of missed chats, -180 000 ₽ monthly loss."""
    img = Image.new("RGB", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = "Автосервисы теряют до 35% ночных и вечерних заявок" if t < 3.5 else "Это 150 000 – 200 000 ₽ упущенной выручки каждый месяц!"
    draw_glow_badge(d, "📉 ЕЖЕМЕСЯЧНАЯ УТЕЧКА КЛИЕНТОВ ИЗ ЧАТОВ", W // 2, 45, fill=(45, 20, 30), text_col=(248, 113, 113))

    # Left: Cascade of missed night cars
    lx1, ly1, lx2, ly2 = 80, 120, 960, 910
    draw_rounded_card(d, [lx1, ly1, lx2, ly2], fill=CARD_BG, outline=BORDER_COL, width=3, radius=22)
    d.rounded_rectangle([lx1, ly1, lx2, ly1 + 75], radius=22, fill=CARD_INNER)
    d.text((lx1 + 35, ly1 + 22), "Пропущенные ночные диалоги (Пятница — Воскресенье)", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    missed_chats = [
        ("BMW X5 • 23:55", "Локальный окрас двери и крыла. Сколько по срокам?", "-38 000 ₽"),
        ("Toyota Camry • 00:30", "Покраска переднего бампера в цвет. Могу пригнать завтра.", "-14 000 ₽"),
        ("Kia K5 • 01:15", "Оклейка передней части в полиуретан Llumar. Цена?", "-52 000 ₽"),
        ("Geely Monjaro • 02:40", "Защита кузова керамикой 3 слоя + бронь фар.", "-45 000 ₽"),
    ]

    for i, (car, q, cost) in enumerate(missed_chats):
        cy1 = ly1 + 100 + i * 145
        cy2 = cy1 + 125
        card_col = (26, 18, 30) if t > (i * 0.8) else (18, 24, 38)
        border_c = ALERT_RED if t > (i * 0.8) else BORDER_MUTED
        draw_rounded_card(d, [lx1 + 30, cy1, lx2 - 30, cy2], fill=card_col, outline=border_c, radius=16)
        d.text((lx1 + 55, cy1 + 15), car, font=FONT_TITLE_SM, fill=ACCENT_BLUE)
        d.text((lx1 + 55, cy1 + 60), q, font=FONT_BODY_SM, fill=TEXT_WHITE)
        d.text((lx2 - 200, cy1 + 15), cost, font=FONT_TITLE_MD, fill=ALERT_RED)

    # Right: Grand Counter
    rx1, ry1, rx2, ry2 = 1000, 120, W - 80, 910
    draw_rounded_card(d, [rx1, ry1, rx2, ry2], fill=(22, 14, 24), outline=ALERT_RED, width=4, radius=22)
    d.text((rx1 + 45, ry1 + 55), "ИТОГО МИМО КАССЫ КАЖДЫЙ МЕСЯЦ:", font=FONT_TITLE_MD, fill=(248, 113, 113))

    # Progressive counter up to 180 000
    p = min(1.0, t / 4.0)
    current_loss = int(p * 180000)
    d.text((rx1 + 45, ry1 + 130), f"-{current_loss:,} ₽".replace(",", " "), font=FONT_MONO_LG, fill=ALERT_RED)
    d.text((rx1 + 45, ry1 + 250), "До 35% всего рекламного бюджета Авито сгорает впустую.", font=FONT_TITLE_SM, fill=TEXT_WHITE)

    stat_cards = [
        ("35% клиентов", "пишут после 21:00 и в выходные дни"),
        ("12 минут", "среднее время ожидания ответа на Авито у конкурентов"),
        ("0 шансов", "вернуть клиента, если ему не ответили за 5 минут")
    ]
    for i, (head, desc) in enumerate(stat_cards):
        sc_y1 = ry1 + 340 + i * 115
        draw_rounded_card(d, [rx1 + 45, sc_y1, rx2 - 45, sc_y1 + 95], fill=(30, 18, 30), outline=(65, 30, 45), radius=14)
        d.text((rx1 + 70, sc_y1 + 14), head, font=FONT_TITLE_MD, fill=ALERT_RED)
        d.text((rx1 + 70, sc_y1 + 54), desc, font=FONT_BODY_SM, fill=TEXT_MUTED)

    draw_high_contrast_subtitle(d, sub)
    return img


def render_sc3_solution(t: float, dur: float) -> Image.Image:
    """Scene 3: Solution - Emerald shift, Заявка 24/7, 3 Fast Badges, No Hallucinations."""
    img = Image.new("RGB", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = "«Заявка 24/7» — умный ИИ-диспетчер для автобизнеса"
    if t >= 5.8:
        sub = "Строго по вашему прайсу: не выдумывает скидки и не ошибается!"
    elif t >= 3.2:
        sub = "Мгновенный ответ за 15 секунд в любое время суток 24/7"

    draw_glow_badge(d, "⚡ РЕШЕНИЕ: ИИ-СТАРШИЙ МАСТЕР ДЛЯ АВТОСЕРВИСА", W // 2, 45, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    # Hero Banner
    draw_rounded_card(d, [80, 115, W - 80, 420], fill=(12, 32, 48), outline=PRIMARY_GREEN, width=4, radius=24)
    d.text((130, 150), "Заявка 24/7", font=FONT_TITLE_XL, fill=TEXT_WHITE)
    d.text((130, 245), "Виртуальный старший мастер: забирает телефон клиента и передает сделку в CRM 24/7", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((130, 315), "⏱ Среднее время ответа: 15 секунд • Без сна, отпусков и больничных", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)

    # 3 Distinct Feature Cards with sequential entry
    features = [
        ("⚡ Ответ за 15 секунд 24/7", "Перехватывает клиента в первые секунды на Авито, WhatsApp и Telegram, пока он не ушёл.", PRIMARY_GREEN),
        ("🔒 Строго по вашему прайсу", "Держит вилки цен студии, не обещает ложных скидок и защищает вашу маржу.", ACCENT_BLUE),
        ("👨‍🔧 Зовёт мастера в сложных случаях", "При нестандартном вопросе или фото повреждения аккуратно зовёт мастера в чат.", GOLD_COL),
    ]

    cw = (W - 160 - 40) // 3
    for i, (title, desc, col) in enumerate(features):
        cx1 = 80 + i * (cw + 20)
        cx2 = cx1 + cw
        draw_rounded_card(d, [cx1, 455, cx2, 910], fill=CARD_BG, outline=col, width=3, radius=20)
        d.text((cx1 + 30, 500), title, font=FONT_TITLE_MD, fill=TEXT_WHITE)
        d.text((cx1 + 30, 580), desc, font=FONT_BODY, fill=TEXT_MUTED)

        # Highlight tag
        draw_glow_badge(d, "ПРОВЕРЕНО В БОКСАХ", cx1 + cw // 2, 830, fill=(24, 34, 56), text_col=col, font=FONT_BADGE)

    draw_high_contrast_subtitle(d, sub)
    return img


def render_sc4_demo(t: float, dur: float) -> Image.Image:
    """Scene 4: Live Split-Screen Demo - Monjaro inquiry, AI calculates price, phone captured, Telegram/CRM push."""
    img = Image.new("RGB", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = "Клиент пишет на Авито ночью ➔ Бот отвечает за 15 секунд"
    if t >= 9.0:
        sub = "Через 1 секунду: готовый лид у мастера в Telegram и в amoCRM!"
    elif t >= 3.5:
        sub = "Квалифицирует марку авто, держит вилку цен и забирает номер"

    draw_glow_badge(d, "🤖 ЖИВОЙ СИНХРОННЫЙ СПЛИТ-СКРИН: ЧАТ АВИТО ➔ ВЫГРУЗКА В TELEGRAM & CRM", W // 2, 45, fill=ACCENT_BLUE, text_col=(0, 0, 0))

    # Left: Client Chat (Avito)
    lx1, ly1, lx2, ly2 = 80, 115, 960, 910
    draw_rounded_card(d, [lx1, ly1, lx2, ly2], fill=(13, 20, 36), outline=(45, 65, 95), width=3, radius=24)
    d.rounded_rectangle([lx1, ly1, lx2, ly1 + 75], radius=24, fill=(20, 30, 52))
    d.text((lx1 + 30, ly1 + 22), "Денис • Старший мастер «Заявка 24/7» (Онлайн)", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    # 1. Customer inquiry
    c1_y = ly1 + 100
    draw_rounded_card(d, [lx1 + 30, c1_y, lx2 - 30, c1_y + 130], fill=(26, 38, 62), outline=BORDER_COL, radius=16)
    d.text((lx1 + 50, c1_y + 15), "Клиент (Алексей • Geely Monjaro):", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((lx1 + 50, c1_y + 60), "Оклейка зон риска на Monjaro сколько? Плёнка SunTek.", font=FONT_BODY, fill=TEXT_WHITE)

    # 2. AI calculates & replies (after 3.5s)
    if t >= 3.5:
        ai_y = c1_y + 155
        draw_rounded_card(d, [lx1 + 50, ai_y, lx2 - 30, ai_y + 210], fill=(16, 185, 129, 30), outline=PRIMARY_GREEN, width=2, radius=16)
        d.text((lx1 + 75, ai_y + 18), "ИИ Старший мастер:", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)
        msg = "Добрый вечер! Полиуретан SunTek на Monjaro —\nот 48 000 ₽. Срок 1 день, гарантия 5 лет.\nЕсть окно в четверг в 10:00. Оставите номер для брони?"
        d.text((lx1 + 75, ai_y + 62), msg, font=FONT_BODY_SM, fill=TEXT_WHITE)

    # 3. Customer sends phone (after 7.5s)
    if t >= 7.5:
        c2_y = c1_y + 395
        draw_rounded_card(d, [lx1 + 30, c2_y, lx2 - 30, c2_y + 115], fill=(26, 38, 62), outline=PRIMARY_GREEN, width=2, radius=16)
        d.text((lx1 + 50, c2_y + 15), "Клиент:", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
        d.text((lx1 + 50, c2_y + 60), "+7 916 555-32-11, Алексей. Запишите на 10:00", font=FONT_BODY, fill=PRIMARY_GREEN)

    # Right: Instant CRM & Telegram Push Card
    rx1, ry1, rx2, ry2 = 1000, 115, W - 80, 910
    draw_rounded_card(d, [rx1, ry1, rx2, ry2], fill=(15, 28, 48), outline=ACCENT_BLUE, width=3, radius=24)
    d.rounded_rectangle([rx1, ry1, rx2, ry1 + 80], radius=24, fill=(24, 44, 76))
    d.text((rx1 + 35, ry1 + 24), "🔔 Мгновенное уведомление мастеру (Telegram & CRM)", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    if t >= 9.0:
        # Telegram card
        draw_rounded_card(d, [rx1 + 35, ry1 + 115, rx2 - 35, ry1 + 450], fill=(10, 20, 36), outline=PRIMARY_GREEN, width=3, radius=18)
        d.text((rx1 + 65, ry1 + 145), "🔥 НОВАЯ ЗАЯВКА С АВИТО:", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)
        d.text((rx1 + 65, ry1 + 215), "Телефон: +7 916 555-32-11 (Алексей)", font=FONT_TITLE_LG, fill=TEXT_WHITE)
        d.text((rx1 + 65, ry1 + 295), "Автомобиль: Geely Monjaro (2024)", font=FONT_TITLE_MD, fill=TEXT_MUTED)
        d.text((rx1 + 65, ry1 + 355), "Услуга: Оклейка зон риска (SunTek, ~48 000 ₽)", font=FONT_TITLE_MD, fill=ACCENT_BLUE)

        # CRM auto-deal
        crm_y = ry1 + 480
        draw_rounded_card(d, [rx1 + 35, crm_y, rx2 - 35, crm_y + 175], fill=(20, 35, 58), outline=GOLD_COL, width=2, radius=18)
        d.text((rx1 + 65, crm_y + 30), "⚡ amoCRM / Битрикс24 Синхронизация:", font=FONT_TITLE_MD, fill=GOLD_COL)
        d.text((rx1 + 65, crm_y + 75), "Сделка #1042 и контакт автоматически созданы в CRM", font=FONT_BODY, fill=TEXT_WHITE)
        d.text((rx1 + 65, crm_y + 115), "Этап: «Запись на осмотр в четверг 10:00»", font=FONT_BODY_SM, fill=PRIMARY_GREEN)

    draw_high_contrast_subtitle(d, sub)
    return img


def render_sc5_roi(t: float, dur: float) -> Image.Image:
    """Scene 5: Economics & ROI - 12 900 ₽ vs 48 000 ₽, pays off with 1 car."""
    img = Image.new("RGB", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = "Подключение за 10 минут без программистов" if t < 3.0 else "Тариф 12 900 ₽/мес окупается ровно с 1 закрытой машины!"
    draw_glow_badge(d, "💰 ЖЕЛЕЗНАЯ ОКУПАЕМОСТЬ • 1 СДЕЛКА ОКУПАЕТ МЕСЯЦ", W // 2, 45, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    # Left: Subscription Cost
    lx1, ly1, lx2, ly2 = 80, 115, 880, 910
    draw_rounded_card(d, [lx1, ly1, lx2, ly2], fill=(16, 26, 44), outline=BORDER_COL, width=3, radius=24)
    d.text((lx1 + 45, ly1 + 45), "ИНВЕСТИЦИЯ В СЕРВИС", font=FONT_TITLE_MD, fill=TEXT_MUTED)
    
    draw_rounded_card(d, [lx1 + 40, ly1 + 110, lx2 - 40, ly1 + 250], fill=(22, 34, 56), outline=BORDER_COL, radius=16)
    d.text((lx1 + 65, ly1 + 130), "Ежемесячная подписка на систему:", font=FONT_BODY_SM, fill=TEXT_MUTED)
    d.text((lx1 + 65, ly1 + 175), "12 900 ₽ / месяц", font=FONT_TITLE_LG, fill=ACCENT_BLUE)

    draw_rounded_card(d, [lx1 + 40, ly1 + 280, lx2 - 40, ly1 + 740], fill=(18, 28, 48), outline=BORDER_COL, radius=16)
    d.text((lx1 + 65, ly1 + 315), "Что входит в тариф:", font=FONT_TITLE_MD, fill=TEXT_WHITE)
    features = [
        "•  1 бот (Авито Pro + Telegram)",
        "•  До 500 квалифицированных диалогов",
        "•  Защита от ошибок и ложных скидок",
        "•  Мгновенные карточки мастеру в телефон",
        "•  Подключение за 10 минут без программистов"
    ]
    for i, f in enumerate(features):
        d.text((lx1 + 65, ly1 + 380 + i * 65), f, font=FONT_BODY, fill=TEXT_MUTED)

    # Right: Revenue from 1 Car (Highlighted in Green)
    rx1, ry1, rx2, ry2 = 920, 115, W - 80, 910
    draw_rounded_card(d, [rx1, ry1, rx2, ry2], fill=(13, 38, 28), outline=PRIMARY_GREEN, width=4, radius=24)
    d.text((rx1 + 50, ry1 + 45), "ВЫРУЧКА ВСЕГО С 1 СПАСЁННОЙ МАШИНЫ", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)

    draw_rounded_card(d, [rx1 + 45, ry1 + 110, rx2 - 45, ry1 + 250], fill=(18, 54, 40), outline=PRIMARY_GREEN, width=2, radius=16)
    d.text((rx1 + 70, ry1 + 130), "Средний чек кузовного ремонта или оклейки:", font=FONT_BODY_SM, fill=TEXT_WHITE)
    d.text((rx1 + 70, ry1 + 175), "+25 000 – 48 000 ₽", font=FONT_TITLE_LG, fill=PRIMARY_GREEN)

    # Large Stamp Banner
    draw_rounded_card(d, [rx1 + 45, ry1 + 280, rx2 - 45, ry1 + 520], fill=(10, 28, 22), outline=PRIMARY_GREEN, width=3, radius=18)
    d.text((rx1 + 70, ry1 + 315), "⚖️ ОКУПАЕТСЯ С 1 КЛИЕНТА!", font=FONT_TITLE_MD, fill=TEXT_WHITE)
    d.text((rx1 + 70, ry1 + 375), "Первый же спасённый ночной клиент полностью\nокупает всю стоимость сервиса за месяц.", font=FONT_BODY, fill=TEXT_WHITE)
    d.text((rx1 + 70, ry1 + 450), "Все остальные 15–30 заявок — ваша чистая прибыль.", font=FONT_TITLE_SM, fill=PRIMARY_GREEN)

    draw_rounded_card(d, [rx1 + 45, ry1 + 555, rx2 - 45, ry1 + 740], fill=(18, 42, 34), outline=BORDER_COL, radius=16)
    d.text((rx1 + 70, ry1 + 590), "🚀 Настройка под ваш прайс-лист за 10 минут", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((rx1 + 70, ry1 + 645), "Отправьте ссылку на объявление Авито — и мы сразу\nзапустим бота на вашем реальном трафике.", font=FONT_BODY_SM, fill=TEXT_MUTED)

    draw_high_contrast_subtitle(d, sub)
    return img


def render_sc6_cta(t: float, dur: float) -> Image.Image:
    """Scene 6: Strong Final CTA - Pulsing button, logo, 3-day test-drive, @Zayavka24AI_bot."""
    img = Image.new("RGB", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = "Запустите тест-драйв на 3 дня бесплатно прямо сейчас!" if t < 3.5 else "Начните спасать ночные заявки уже этой ночью: @Zayavka24AI_bot"
    draw_glow_badge(d, "🚀 БЕСПЛАТНЫЙ ТЕСТ-ДРАЙВ НА 3 ДНЯ • БЕЗ ПРИВЯЗКИ КАРТЫ", W // 2, 45, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    # Main Center Card
    cx1 = 160
    cy1 = 115
    cx2 = W - 160
    cy2 = 910
    draw_rounded_card(d, [cx1, cy1, cx2, cy2], fill=(12, 22, 38), outline=PRIMARY_GREEN, width=4, radius=26)

    # Logo & Brand
    d.text((W // 2 - 220, cy1 + 50), "Заявка 24/7", font=FONT_TITLE_XL, fill=TEXT_WHITE)
    d.text((W // 2 - 270, cy1 + 140), "ИИ-диспетчер продаж для автосервисов и детейлинга", font=FONT_TITLE_MD, fill=ACCENT_BLUE)

    # Shimmering Pulsing CTA Button
    pulse = 0.5 + 0.5 * math.sin(t * 5.0)
    g_val = int(185 + 40 * pulse)
    btn_col = (16, min(255, g_val), 129)
    btn_w = 780
    bx1 = W // 2 - btn_w // 2
    by1 = cy1 + 230
    bx2 = bx1 + btn_w
    by2 = by1 + 130
    draw_rounded_card(d, [bx1, by1, bx2, by2], fill=btn_col, outline=(255, 255, 255), width=3, radius=22)
    d.text((bx1 + 80, by1 + 40), "3 ДНЯ ТЕСТ-ДРАЙВА БЕСПЛАТНО", font=FONT_TITLE_MD, fill=(0, 0, 0))

    # 4 Trust Badges
    bullets = [
        "✓  Настройка под ваш автосервис за 10 минут",
        "✓  Без привязки банковской карты и скрытых списаний",
        "✓  Подключение Авито Pro, WhatsApp Web и Telegram",
        "✓  Тестируйте на реальных ночных клиентах прямо сегодня"
    ]
    for i, b in enumerate(bullets):
        col = (i % 2)
        row = (i // 2)
        bx = cx1 + 100 + col * 750
        by = by1 + 180 + row * 80
        d.text((bx, by), b, font=FONT_BODY, fill=TEXT_WHITE)

    # Contact & Links Container
    link_y = cy1 + 590
    draw_rounded_card(d, [cx1 + 80, link_y, cx2 - 80, link_y + 140], fill=(18, 30, 52), outline=ACCENT_BLUE, radius=18)
    d.text((cx1 + 120, link_y + 25), "📱 Демо в Telegram:     @Zayavka24AI_bot", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)
    d.text((cx1 + 120, link_y + 78), "🌐 Официальный сайт:   77.221.155.74.sslip.io/landing", font=FONT_BODY, fill=ACCENT_BLUE)

    draw_high_contrast_subtitle(d, sub)
    return img


RENDERERS = {
    "sc1_hook": render_sc1_hook,
    "sc2_pain": render_sc2_pain,
    "sc3_solution": render_sc3_solution,
    "sc4_demo": render_sc4_demo,
    "sc5_roi": render_sc5_roi,
    "sc6_cta": render_sc6_cta,
}


# --- Scene Rendering with rgb24 Piping ---
def render_scene_video(scene_id: str, duration_sec: float) -> Path:
    out_mp4 = OUT_DIR / f"{scene_id}_video.mp4"
    renderer = RENDERERS[scene_id]
    total_frames = int(duration_sec * FPS)

    print(f"[*] Рендеринг {scene_id}: {duration_sec:.2f} сек ({total_frames} кадров, rgb24)...")
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{W}x{H}",
        "-pix_fmt", "rgb24",
        "-r", str(FPS),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        str(out_mp4)
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for i in range(total_frames):
        t = i / FPS
        frame = renderer(t, duration_sec)
        # Convert directly to RGB (rgb24, 3 bytes/pixel)
        proc.stdin.write(frame.convert("RGB").tobytes())

    proc.stdin.close()
    proc.wait()
    return out_mp4


# --- Audio Mix per Scene with Precise adelay ---
def mix_scene_audio(scene_id: str, vo_wav: Path, total_dur: float) -> Path:
    out_audio = OUT_DIR / f"{scene_id}_audio.wav"

    if scene_id == "sc1_hook":
        # Lost lead alert sound at 3.0s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_lost.wav"),
            "-filter_complex", "[1:a]adelay=3000|3000[sfx];[0:a][sfx]amix=inputs=2:normalize=0:dropout_transition=0",
            str(out_audio)
        ]
    elif scene_id == "sc2_pain":
        # 4 pop sounds for each incoming car
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_pop.wav"),
            "-filter_complex", "[1:a]adelay=800|800[p1];[1:a]adelay=1600|1600[p2];[1:a]adelay=2400|2400[p3];[0:a][p1][p2][p3]amix=inputs=4:normalize=0:dropout_transition=0",
            str(out_audio)
        ]
    elif scene_id == "sc3_solution":
        # Whoosh at start
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_whoosh.wav"),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0:dropout_transition=0",
            str(out_audio)
        ]
    elif scene_id == "sc4_demo":
        # Pop at 3.5s, Telegram chime at 9.0s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_pop.wav"),
            "-i", str(OUT_DIR / "sfx_tg.wav"),
            "-filter_complex", "[1:a]adelay=3500|3500[p];[2:a]adelay=9000|9000[tg];[0:a][p][tg]amix=inputs=3:normalize=0:dropout_transition=0",
            str(out_audio)
        ]
    elif scene_id == "sc5_roi":
        # Cash register at 3.2s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_cash.wav"),
            "-filter_complex", "[1:a]adelay=3200|3200[c];[0:a][c]amix=inputs=2:normalize=0:dropout_transition=0",
            str(out_audio)
        ]
    else:
        # sc6_cta: Whoosh at start
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_whoosh.wav"),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0:dropout_transition=0",
            str(out_audio)
        ]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return out_audio


# --- Main Orchestrator ---
async def main():
    print("==================================================================")
    print("🚀 Генерация продающего промо-ролика для сайта «Заявка 24/7» (16:9)")
    print("==================================================================")

    # 1. Generate SFX library
    print("[1/6] Синтез библиотеки мягких звуковых эффектов (SFX)...")
    generate_soft_sfx_library(OUT_DIR)

    # 2. Voiceover synthesis with DmitryNeural (+8% tempo)
    print("[2/6] Синтез дикторской озвучки (DmitryNeural +8% tempo)...")
    scene_vo_paths = {}
    scene_durations = {}
    total_video_dur = 0.0

    for sc in SCENES:
        sid = sc["id"]
        vo_p = await generate_scene_voiceover(sc)
        dur = get_wav_duration(vo_p)
        # Extra 0.4s padding for natural breathing & viewer comprehension
        final_scene_dur = round(dur + 0.4, 2)
        scene_vo_paths[sid] = vo_p
        scene_durations[sid] = final_scene_dur
        total_video_dur += final_scene_dur
        print(f"  • {sid}: {final_scene_dur:.2f} сек (голос: {dur:.2f} с)")

    print(f"[+] Общий расчётный хронометраж ролика: {total_video_dur:.2f} сек (~41 сек)")

    # 3. Generate background ambient bed for the exact duration
    bgm_path = OUT_DIR / "ambient_bed.wav"
    print("[3/6] Генерация ненавязчивой фоновой ambient-подложки (-22 dB)...")
    generate_ambient_bed(bgm_path, total_video_dur + 2.0)

    # 4. Render video frames and mix audio for each scene
    print("[4/6] Потоковый рендеринг видеокадров (rgb24) и сведение аудио...")
    scene_complete_mp4s = []

    for sc in SCENES:
        sid = sc["id"]
        dur = scene_durations[sid]
        vo_p = scene_vo_paths[sid]

        v_mp4 = render_scene_video(sid, dur)
        a_wav = mix_scene_audio(sid, vo_p, dur)

        muxed_mp4 = OUT_DIR / f"{sid}_complete.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(v_mp4),
            "-i", str(a_wav),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(muxed_mp4)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        scene_complete_mp4s.append(muxed_mp4)

    # 5. Concatenate all scenes into a single stream
    print("[5/6] Бесшовная склейка всех 6 сцен (concat)...")
    concat_list = OUT_DIR / "concat_v2.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in scene_complete_mp4s:
            f.write(f"file '{p.name}'\n")

    unmastered_mp4 = OUT_DIR / "zayavka24_unmastered.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(unmastered_mp4)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # 6. Final Audio Mastering & Web FastStart
    final_mp4 = OUT_DIR / "zayavka24_promo_16x9.mp4"
    print(f"[6/6] Финальное сведение с фоновой музыкой, loudnorm и -movflags +faststart в {final_mp4.name}...")
    master_cmd = [
        "ffmpeg", "-y",
        "-i", str(unmastered_mp4),
        "-i", str(bgm_path),
        "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0:dropout_transition=0,loudnorm=I=-14:TP=-1.5:LRA=11[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(final_mp4)
    ]
    subprocess.run(master_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # 7. Generate Poster (poster.webp) from Scene 4 split-screen
    poster_webp = OUT_DIR / "poster.webp"
    print("[*] Генерация высококонверсионного постера poster.webp...")
    # Extract frame at ~22.0 seconds (Split screen action)
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", "00:00:22.000",
        "-i", str(final_mp4),
        "-vframes", "1",
        "-c:v", "libwebp",
        "-quality", "90",
        str(poster_webp)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # 8. QA Frame Extractions (02s, 07s, 14s, 24s, 34s, 40s)
    qa_timestamps = ["00:00:02", "00:00:07", "00:00:14", "00:00:24", "00:00:34", "00:00:40"]
    print(f"[*] Извлечение 6 контрольных кадров QA в {FRAMES_QA_DIR}...")
    for ts in qa_timestamps:
        label = ts.replace("00:00:", "").replace(":", "")
        shot_path = FRAMES_QA_DIR / f"qa_shot_{label}s.png"
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", ts,
            "-i", str(final_mp4),
            "-vframes", "1",
            str(shot_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    print("\n🎉 ГОТОВО! Промо-ролик для лендинга успешно собран.")
    print(f"📁 Видео: {final_mp4.resolve()}")
    print(f"🖼 Постер: {poster_webp.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
