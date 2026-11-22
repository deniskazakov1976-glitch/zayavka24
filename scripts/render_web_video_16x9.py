import asyncio
import math
import struct
import subprocess
import wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import edge_tts

W, H = 1920, 1080  # Full HD 16:9 Widescreen
FPS = 30
OUT_DIR = Path("video_explainer")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Fonts
FONTS_DIR = Path("C:\\Windows\\Fonts")
FONT_TITLE_XL = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 54)
FONT_TITLE_LG = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 42)
FONT_TITLE_MD = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 32)
FONT_TITLE_SM = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 30)
FONT_BODY = ImageFont.truetype(str(FONTS_DIR / "segoeui.ttf"), 28)
FONT_BODY_SM = ImageFont.truetype(str(FONTS_DIR / "segoeui.ttf"), 22)
FONT_BADGE = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 24)
FONT_SUBTITLE = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 36)

# Palette
BG_DARK = (8, 14, 26)
CARD_BG = (15, 23, 42)
CARD_INNER = (24, 34, 56)
BORDER_COL = (33, 45, 69)
PRIMARY_GREEN = (16, 185, 129)
ACCENT_BLUE = (56, 189, 248)
WARNING_RED = (239, 68, 68)
GOLD_COL = (245, 158, 11)
TEXT_WHITE = (255, 255, 255)
TEXT_MUTED = (148, 163, 184)

WEB_SCENES = [
    {
        "id": "web_sc1",
        "text": "Каждый вечер после закрытия автосервиса вы теряете от трёх до семи горячих клиентов на Авито и в WhatsApp. Мастера заняты или спят, а клиент не ждёт и уходит к конкурентам.",
        "subs": [
            (0.0, 4.0, "Вечер. Автосервис закрыт, мастера спят."),
            (4.0, 8.5, "Клиент не дожидается ответа и уходит к конкурентам."),
            (8.5, 12.0, "Каждую неделю вы теряете 3–7 горячих клиентов!")
        ]
    },
    {
        "id": "web_sc2",
        "text": "ИИ-менеджер «Заявка 24/7» решает эту проблему навсегда. Он отвечает клиенту за 15 секунд в любое время суток, как опытный старший мастер.",
        "subs": [
            (0.0, 4.2, "ИИ-менеджер «Заявка 24/7» спасает каждую сделку."),
            (4.2, 8.5, "Ответ за 15 секунд в любое время суток!"),
            (8.5, 11.5, "Общается как опытный старший мастер.")
        ]
    },
    {
        "id": "web_sc3",
        "text": "Бот сам оценивает деталь прямо по фото, держит вилку цен строго по вашему прайсу, забирает телефон и мгновенно передаёт готовую сделку в ваш Телеграм и CRM.",
        "subs": [
            (0.0, 3.8, "Оценивает деталь и марку авто по фото."),
            (3.8, 7.5, "Держит вилку цен строго по вашему прайсу."),
            (7.5, 11.5, "Забирает номер телефона и пересылает в Telegram и CRM!")
        ]
    },
    {
        "id": "web_sc4",
        "text": "Подписка окупается ровно с одного заехавшего автомобиля. Запустите тест-драйв на три дня бесплатно: настроим под ваш автосервис за десять минут прямо на сайте.",
        "subs": [
            (0.0, 4.0, "Окупается ровно с 1 заехавшего автомобиля!"),
            (4.0, 7.5, "Запустите тест-драйв на 3 дня бесплатно."),
            (7.5, 11.5, "Настроим под ваш автосервис за 10 минут прямо на сайте!")
        ]
    }
]


def write_wav(filename: Path, samples: list[float], sample_rate: int = 44100):
    with wave.open(str(filename), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        raw = bytearray()
        for s in samples:
            val = max(-1.0, min(1.0, s))
            raw.extend(struct.pack("<h", int(val * 32767)))
        wav.writeframes(raw)


def draw_card(draw, bbox, fill=CARD_BG, outline=BORDER_COL, width=2, radius=20):
    draw.rounded_rectangle(bbox, radius=radius, fill=fill, outline=outline, width=width)


def draw_badge(draw, text, cx, y, fill=PRIMARY_GREEN, text_col=(0, 0, 0), font=FONT_BADGE):
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    px, py = 18, 8
    draw.rounded_rectangle([cx - tw // 2 - px, y, cx + tw // 2 + px, y + th + py * 2], radius=10, fill=fill)
    draw.text((cx - tw // 2, y + py - 2), text, font=font, fill=text_col)


def draw_subtitle(draw, text):
    if not text:
        return
    bbox = FONT_SUBTITLE.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    cx = W // 2
    y = H - 95
    px, py = 24, 10
    draw.rounded_rectangle([cx - tw // 2 - px, y - py, cx + tw // 2 + px, y + th + py], radius=14, fill=(5, 10, 20, 230), outline=(56, 189, 248, 160), width=2)
    draw.text((cx - tw // 2, y - 3), text, font=FONT_SUBTITLE, fill=(255, 255, 255))


def get_wav_dur(p: Path) -> float:
    with wave.open(str(p), "r") as wf:
        return wf.getnframes() / float(wf.getframerate())


# === Scene 1 Frame (Widescreen Problem) ===
def render_web_sc1(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 4.0:
        sub = "Вечер. Автосервис закрыт, мастера спят."
    elif t < 8.5:
        sub = "Клиент не дожидается ответа и уходит к конкурентам."
    else:
        sub = "Каждую неделю автосервис теряет 3–7 горячих клиентов!"

    # Header Top Bar
    draw_badge(d, "🌙 22:45 • АВТОСЕРВИС ЗАКРЫТ • МАСТЕРА СПЯТ", W // 2, 45, fill=(30, 41, 59), text_col=TEXT_MUTED)

    # Split Screen: Left (Avito unresponded chat)
    lx1, ly1, lx2, ly2 = 80, 120, 920, 920
    draw_card(d, [lx1, ly1, lx2, ly2], fill=CARD_BG, outline=BORDER_COL, width=3, radius=24)
    d.rounded_rectangle([lx1, ly1, lx2, ly1 + 75], radius=24, fill=CARD_INNER)
    d.text((lx1 + 35, ly1 + 22), "Авито Сообщения • Кузовной ремонт и покраска", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    # Incoming message
    draw_card(d, [lx1 + 35, ly1 + 115, lx2 - 35, ly1 + 270], fill=(24, 34, 56), outline=(45, 60, 90), radius=16)
    d.text((lx1 + 60, ly1 + 135), "Клиент (Toyota Camry):", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((lx1 + 60, ly1 + 185), "Сколько стоит локальная покраска переднего бампера?\nПритёрли на парковке, могу скинуть фото.", font=FONT_BODY, fill=TEXT_WHITE)

    # Waiting timer
    if t > 3.0:
        prog = min(1.0, (t - 3.0) / 4.0)
        secs = int(prog * 180)
        draw_card(d, [lx1 + 35, ly1 + 300, lx2 - 35, ly1 + 420], fill=(28, 18, 28), outline=(239, 68, 68, 180), radius=16)
        d.text((lx1 + 60, ly1 + 325), f"⏱ Ожидание ответа: 0{secs // 60}:{secs % 60:02d} мин  (мастера не отвечают...)", font=FONT_TITLE_MD, fill=(248, 113, 113))

    if t > 6.5:
        draw_card(d, [lx1 + 35, ly1 + 450, lx2 - 35, ly1 + 540], fill=(36, 16, 24), outline=WARNING_RED, radius=16)
        d.text((lx1 + 60, ly1 + 475), "❌ Клиент закрыл чат и ушёл к конкуренту", font=FONT_TITLE_MD, fill=WARNING_RED)

    # Right Column: Big Financial Loss Analytics
    rx1, ry1, rx2, ry2 = 1000, 120, W - 80, 920
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(22, 14, 24), outline=WARNING_RED, width=3, radius=24)
    d.text((rx1 + 45, ry1 + 50), "УПУЩЕННАЯ ВЫРУЧКА АВТОСЕРВИСА", font=FONT_TITLE_MD, fill=(248, 113, 113))
    d.text((rx1 + 45, ry1 + 120), "-150 000 ₽", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 88), fill=WARNING_RED)
    d.text((rx1 + 45, ry1 + 250), "Каждую неделю после 21:00 вы теряете 3–7 горячих клиентов.", font=FONT_TITLE_SM, fill=TEXT_WHITE)

    stat_bullets = [
        "•  82% клиентов пишут сразу в 3–5 сервисов на Авито",
        "•  Кто ответил за 1–2 минуты — забирает машину на осмотр",
        "•  Мастер занят работой или спит — деньги уходят конкуренту",
    ]
    for i, b in enumerate(stat_bullets):
        d.text((rx1 + 45, ry1 + 350 + i * 80), b, font=FONT_BODY, fill=(226, 232, 240))

    draw_subtitle(d, sub)
    return img


# === Scene 2 Frame (Widescreen Solution & Speed) ===
def render_web_sc2(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 4.2:
        sub = "ИИ-менеджер «Заявка 24/7» спасает каждую сделку."
    elif t < 8.5:
        sub = "Ответ за 15 секунд в любое время суток!"
    else:
        sub = "Общается как опытный старший мастер."

    draw_badge(d, "⚡ ИИ-ДИСПЕТЧЕР ДЛЯ АВИТО, WHATSAPP И TELEGRAM", W // 2, 45, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    # Main Hero Solution Banner
    draw_card(d, [80, 120, W - 80, 440], fill=(12, 30, 48), outline=PRIMARY_GREEN, width=3, radius=24)
    d.text((130, 160), "Заявка 24/7", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 72), fill=TEXT_WHITE)
    d.text((130, 255), "Виртуальный старший мастер: забирает телефон клиента и передает в CRM 24/7", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((130, 325), "⏱ Среднее время ответа: 15 секунд • Без сна, отпусков и больничных", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)

    # 3 Large Feature Columns
    cols = [
        ("💬 Живой диалог мастера", "Не спамит шаблонами, завершает каждое сообщение вопросом, ведущим к записи в бокс."),
        ("📸 Оценка по фото и авто", "Распознает марку и сложность повреждения детали прямо по присланному фото."),
        ("🔒 Защита цен (Guardrails)", "Держит вилку цен строго по вашему прайсу и никогда не выдумывает ложные скидки."),
    ]
    cw = (W - 160 - 40) // 3
    for i, (col_t, col_b) in enumerate(cols):
        cx1 = 80 + i * (cw + 20)
        cx2 = cx1 + cw
        draw_card(d, [cx1, 470, cx2, 920], fill=CARD_BG, outline=BORDER_COL, width=2, radius=20)
        d.text((cx1 + 30, 510), col_t, font=FONT_TITLE_MD, fill=TEXT_WHITE)
        d.text((cx1 + 30, 590), col_b, font=FONT_BODY, fill=TEXT_MUTED)

    draw_subtitle(d, sub)
    return img


# === Scene 3 Frame (Widescreen Live Split Screen Action) ===
def render_web_sc3(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 3.8:
        sub = "Оценивает деталь и марку авто по фото."
    elif t < 7.5:
        sub = "Держит вилку цен строго по вашему прайсу."
    else:
        sub = "Забирает номер телефона и пересылает готовую сделку в Telegram и CRM!"

    draw_badge(d, "🤖 ЖИВОЙ ДИАЛОГ С КЛИЕНТОМ ➔ АВТОВЫГРУЗКА В TELEGRAM & CRM", W // 2, 45, fill=ACCENT_BLUE, text_col=(0, 0, 0))

    # Left: Client Chat Window
    lx1, ly1, lx2, ly2 = 80, 115, 960, 920
    draw_card(d, [lx1, ly1, lx2, ly2], fill=(13, 20, 36), outline=(40, 56, 85), width=3, radius=24)
    d.rounded_rectangle([lx1, ly1, lx2, ly1 + 75], radius=24, fill=(20, 30, 52))
    d.text((lx1 + 30, ly1 + 22), "Денис • Старший мастер «Заявка 24/7» (Онлайн)", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    # 1. Customer bubble
    c1_y = ly1 + 105
    draw_card(d, [lx1 + 30, c1_y, lx2 - 30, c1_y + 130], fill=(26, 38, 62), outline=BORDER_COL, radius=16)
    d.text((lx1 + 50, c1_y + 15), "Клиент (Geely Monjaro):", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((lx1 + 50, c1_y + 60), "Оклейка зон риска на Monjaro сколько? Плёнка Llumar.", font=FONT_BODY, fill=TEXT_WHITE)

    # 2. AI Reply bubble
    if t >= 3.5:
        ai_y = c1_y + 155
        draw_card(d, [lx1 + 60, ai_y, lx2 - 30, ai_y + 200], fill=(16, 185, 129, 28), outline=PRIMARY_GREEN, width=2, radius=16)
        d.text((lx1 + 80, ai_y + 18), "ИИ Старший мастер:", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)
        msg = "Здравствуйте! На Monjaro оклейка зон риска\nполиуретаном (Llumar) — от 45 000 до 75 000 ₽.\nГарантия 5 лет. На какой день записать на осмотр?"
        d.text((lx1 + 80, ai_y + 62), msg, font=FONT_BODY_SM, fill=TEXT_WHITE)

    # 3. Customer response with phone
    if t >= 7.0:
        c2_y = c1_y + 380
        draw_card(d, [lx1 + 30, c2_y, lx2 - 30, c2_y + 110], fill=(26, 38, 62), outline=BORDER_COL, radius=16)
        d.text((lx1 + 50, c2_y + 15), "Клиент:", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
        d.text((lx1 + 50, c2_y + 58), "Запишите на субботу. +7 999 111-22-33", font=FONT_BODY, fill=PRIMARY_GREEN)

    # Right: Instant CRM & Telegram Push Card
    rx1, ry1, rx2, ry2 = 1000, 115, W - 80, 920
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(15, 28, 48), outline=ACCENT_BLUE, width=3, radius=24)
    
    # Telegram Push Notification Header
    d.rounded_rectangle([rx1, ry1, rx2, ry1 + 80], radius=24, fill=(24, 44, 76))
    d.text((rx1 + 35, ry1 + 24), "🔔 Мгновенное уведомление в Telegram мастера", font=FONT_TITLE_MD, fill=TEXT_WHITE)

    if t >= 8.5:
        draw_card(d, [rx1 + 35, ry1 + 115, rx2 - 35, ry1 + 430], fill=(10, 20, 36), outline=PRIMARY_GREEN, width=2, radius=18)
        d.text((rx1 + 65, ry1 + 145), "НОВЫЙ ЛИД С АВИТО:", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)
        d.text((rx1 + 65, ry1 + 215), "Телефон: +7 999 111-22-33", font=FONT_TITLE_LG, fill=TEXT_WHITE)
        d.text((rx1 + 65, ry1 + 285), "Автомобиль: Geely Monjaro (2024)", font=FONT_TITLE_MD, fill=TEXT_MUTED)
        d.text((rx1 + 65, ry1 + 345), "Услуга: Оклейка зон риска (вилка 45 000 – 75 000 ₽)", font=FONT_TITLE_MD, fill=ACCENT_BLUE)

        # CRM sync badge
        crm_y = ry1 + 465
        draw_card(d, [rx1 + 35, crm_y, rx2 - 35, crm_y + 160], fill=(20, 35, 58), outline=BORDER_COL, radius=18)
        d.text((rx1 + 65, crm_y + 35), "⚡ amoCRM / Битрикс24 Синхронизация:", font=FONT_TITLE_MD, fill=GOLD_COL)
        d.text((rx1 + 65, crm_y + 85), "Сделка #1042 и контакт автоматически созданы в CRM", font=FONT_BODY, fill=TEXT_WHITE)

    draw_subtitle(d, sub)
    return img


# === Scene 4 Frame (Widescreen ROI & CTA) ===
def render_web_sc4(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 4.0:
        sub = "Окупается ровно с 1 заехавшего автомобиля!"
    elif t < 7.5:
        sub = "Запустите тест-драйв на 3 дня бесплатно."
    else:
        sub = "Настроим под ваш автосервис за 10 минут прямо на сайте!"

    draw_badge(d, "🚀 НАЧНИТЕ ПРЯМО СЕЙЧАС • ТЕСТ-ДРАЙВ 3 ДНЯ БЕСПЛАТНО", W // 2, 45, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    # Left: Economics
    lx1, ly1, lx2, ly2 = 80, 115, 880, 920
    draw_card(d, [lx1, ly1, lx2, ly2], fill=(16, 26, 44), outline=GOLD_COL, width=3, radius=24)
    d.text((lx1 + 45, ly1 + 45), "ЭКОНОМИКА И ОКУПАЕМОСТЬ", font=FONT_TITLE_MD, fill=GOLD_COL)
    
    # 1. Cost
    draw_card(d, [lx1 + 40, ly1 + 110, lx2 - 40, ly1 + 250], fill=(22, 34, 56), outline=BORDER_COL, radius=16)
    d.text((lx1 + 65, ly1 + 130), "Инвестиция в сервис:", font=FONT_BODY_SM, fill=TEXT_MUTED)
    d.text((lx1 + 65, ly1 + 170), "12 900 ₽ / месяц", font=FONT_TITLE_LG, fill=ACCENT_BLUE)

    # 2. Revenue from 1 car
    draw_card(d, [lx1 + 40, ly1 + 280, lx2 - 40, ly1 + 420], fill=(13, 38, 28), outline=PRIMARY_GREEN, width=2, radius=16)
    d.text((lx1 + 65, ly1 + 300), "1 закрытая машина на ремонт:", font=FONT_BODY_SM, fill=PRIMARY_GREEN)
    d.text((lx1 + 65, ly1 + 340), "+45 000 – 75 000 ₽", font=FONT_TITLE_LG, fill=PRIMARY_GREEN)

    # 3. Scale summary
    draw_card(d, [lx1 + 40, ly1 + 450, lx2 - 40, ly1 + 750], fill=(24, 32, 52), outline=BORDER_COL, radius=16)
    d.text((lx1 + 65, ly1 + 480), "⚖️ Окупается с 1 закрытого клиента!", font=FONT_TITLE_MD, fill=TEXT_WHITE)
    body = (
        "Каждую неделю ночью и в выходные вы теряете\n"
        "3–5 клиентов, пока мастера заняты.\n\n"
        "«Заявка 24/7» спасает каждую сделку и приносит\n"
        "чистую прибыль уже с первого дня."
    )
    d.text((lx1 + 65, ly1 + 540), body, font=FONT_BODY_SM, fill=TEXT_MUTED)

    # Right: Hero CTA & Link
    rx1, ry1, rx2, ry2 = 920, 115, W - 80, 920
    draw_card(d, [rx1, ry1, rx2, ry2], fill=(12, 25, 42), outline=PRIMARY_GREEN, width=4, radius=24)
    
    d.text((rx1 + 50, ry1 + 55), "Заявка 24/7", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 68), fill=TEXT_WHITE)
    d.text((rx1 + 50, ry1 + 145), "ИИ-диспетчер продаж для автобизнеса", font=FONT_TITLE_MD, fill=ACCENT_BLUE)

    # Pulsing Button
    pulse = 0.5 + 0.5 * math.sin(t * 6)
    btn_col = (16 + int(20 * pulse), 185, 129)
    btn_y = ry1 + 225
    draw_card(d, [rx1 + 50, btn_y, rx2 - 50, btn_y + 120], fill=btn_col, outline=(255, 255, 255), width=2, radius=18)
    d.text((rx1 + 105, btn_y + 35), "ТЕСТ-ДРАЙВ 3 ДНЯ БЕСПЛАТНО", font=FONT_TITLE_MD, fill=(0, 0, 0))

    cta_bullets = [
        "✓  Настройка под ваш прайс за 10 минут",
        "✓  Без привязки карты и скрытых платежей",
        "✓  Подключение Авито, WhatsApp и Telegram",
        "✓  Готовые лиды сразу у мастера в телефоне",
    ]
    for i, b in enumerate(cta_bullets):
        d.text((rx1 + 50, btn_y + 175 + i * 65), b, font=FONT_BODY, fill=TEXT_WHITE)

    # Link Pill
    link_y = ry1 + 610
    draw_card(d, [rx1 + 50, link_y, rx2 - 50, link_y + 140], fill=(20, 34, 58), outline=ACCENT_BLUE, radius=18)
    d.text((rx1 + 80, link_y + 25), "🌐 Официальный сайт:  77.221.155.74.sslip.io/landing", font=FONT_TITLE_MD, fill=ACCENT_BLUE)
    d.text((rx1 + 80, link_y + 75), "📱 Демо в Telegram:     @Zayavka24AI_bot", font=FONT_BODY, fill=PRIMARY_GREEN)

    draw_subtitle(d, sub)
    return img


RENDERERS = {
    "web_sc1": render_web_sc1,
    "web_sc2": render_web_sc2,
    "web_sc3": render_web_sc3,
    "web_sc4": render_web_sc4,
}


async def gen_web_voiceovers():
    print("[*] Генерация озвучки для веб-ролика (16:9)...")
    durs = {}
    for sc in WEB_SCENES:
        sid = sc["id"]
        mp3_p = OUT_DIR / f"{sid}_vo.mp3"
        wav_p = OUT_DIR / f"{sid}_vo.wav"

        c = edge_tts.Communicate(sc["text"], "ru-RU-SvetlanaNeural")
        await c.save(str(mp3_p))

        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_p), "-ar", "44100", "-ac", "1", str(wav_p)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        dur = get_wav_dur(wav_p)
        durs[sid] = round(dur, 2)
        print(f"[+] {sid}: {dur:.2f} сек")
    return durs


def render_scene(sid: str, dur: float):
    print(f"[*] Рендеринг видео 16:9 для {sid} ({dur:.2f} с)...")
    renderer = RENDERERS[sid]
    total_frames = int(dur * FPS)

    v_mp4 = OUT_DIR / f"{sid}_video.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{W}x{H}",
        "-pix_fmt", "rgba",
        "-r", str(FPS),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(v_mp4)
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for i in range(total_frames):
        t = i / FPS
        frame = renderer(t, dur)
        proc.stdin.write(frame.tobytes())

    proc.stdin.close()
    proc.wait()
    return v_mp4


def mix_audio(sid: str):
    vo_wav = OUT_DIR / f"{sid}_vo.wav"
    out_audio = OUT_DIR / f"{sid}_audio.wav"

    if sid == "web_sc1":
        # Add warning alert sound at 6.5s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_lost.wav"),
            "-filter_complex", "[1:a]adelay=6500|6500[sfx];[0:a][sfx]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    elif sid == "web_sc2":
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_whoosh.wav"),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    elif sid == "web_sc3":
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_pop.wav"),
            "-i", str(OUT_DIR / "sfx_telegram.wav"),
            "-filter_complex", "[1:a]adelay=3500|3500[p];[2:a]adelay=8500|8500[tg];[0:a][p][tg]amix=inputs=3:duration=first",
            str(out_audio)
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_cash.wav"),
            "-filter_complex", "[1:a]adelay=4000|4000[c];[0:a][c]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return out_audio


def main():
    durs = asyncio.run(gen_web_voiceovers())
    print("Длительности сцен веб-ролика:", durs)

    combined = []
    for sc in WEB_SCENES:
        sid = sc["id"]
        dur = durs[sid]
        v_p = render_scene(sid, dur)
        a_p = mix_audio(sid)

        muxed = OUT_DIR / f"{sid}_complete.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(v_p),
            "-i", str(a_p),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(muxed)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        combined.append(muxed)

    concat_txt = OUT_DIR / "concat_web.txt"
    with open(concat_txt, "w") as f:
        for s in combined:
            f.write(f"file '{s.name}'\n")

    final_web_mp4 = OUT_DIR / "zayavka24_web_16x9_48s.mp4"
    print(f"[*] Сборка веб-ролика 16:9 в {final_web_mp4}...")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_txt),
        "-c", "copy",
        str(final_web_mp4)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    print(f"\n🎉 ВЕБ-РОЛИК 16:9 ГОТОВ: {final_web_mp4.resolve()}")


if __name__ == "__main__":
    main()
