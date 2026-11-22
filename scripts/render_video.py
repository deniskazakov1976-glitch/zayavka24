import math
import os
import subprocess
import wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FPS = 30
OUT_DIR = Path("video_explainer")

# Windows fonts
FONTS_DIR = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
FONT_TITLE_LG = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 56)
FONT_TITLE_MD = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 44)
FONT_TITLE_SM = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 34)
FONT_BODY = ImageFont.truetype(str(FONTS_DIR / "segoeui.ttf"), 32)
FONT_BODY_SM = ImageFont.truetype(str(FONTS_DIR / "segoeui.ttf"), 26)
FONT_BADGE = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 26)
FONT_SUBTITLE = ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 36)

# Colors
BG_DARK = (10, 15, 29)
CARD_BG = (19, 29, 49)
BORDER_COL = (36, 48, 74)
PRIMARY_GREEN = (16, 185, 129)
ACCENT_BLUE = (56, 189, 248)
WARNING_RED = (239, 68, 68)
TEXT_WHITE = (255, 255, 255)
TEXT_MUTED = (148, 163, 184)
GOLD_COL = (245, 158, 11)


def get_wav_duration(p: Path) -> float:
    with wave.open(str(p), "r") as wf:
        return wf.getnframes() / float(wf.getframerate())


def draw_card(draw, bbox, fill=CARD_BG, outline=BORDER_COL, width=2, radius=24):
    draw.rounded_rectangle(bbox, radius=radius, fill=fill, outline=outline, width=width)


def draw_badge(draw, text, cx, y, fill=PRIMARY_GREEN, text_col=(0, 0, 0), font=FONT_BADGE):
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    px, py = 22, 10
    card_box = [cx - tw // 2 - px, y, cx + tw // 2 + px, y + th + py * 2]
    draw.rounded_rectangle(card_box, radius=12, fill=fill)
    draw.text((cx - tw // 2, y + py - 2), text, font=font, fill=text_col)


def draw_subtitle(draw, text):
    if not text:
        return
    bbox = FONT_SUBTITLE.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    cx = W // 2
    y = H - 180
    px, py = 28, 14
    box = [cx - tw // 2 - px, y - py, cx + tw // 2 + px, y + th + py]
    draw.rounded_rectangle(box, radius=16, fill=(5, 10, 20, 235), outline=(56, 189, 248, 180), width=2)
    draw.text((cx - tw // 2, y - 4), text, font=FONT_SUBTITLE, fill=(255, 255, 255))


def render_scene1_frame(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    # Subtitles logic
    sub = ""
    if t < 3.2:
        sub = "Суббота. 22:30 вечера."
    elif t < 6.0:
        sub = "Автосервис закрыт. Мастера спят."
    elif t < 10.5:
        sub = "Клиент ждёт три минуты и уходит к конкурентам..."
    else:
        sub = "Вы только что потеряли 45 000 рублей!"

    # Header Night Badge
    draw_badge(d, "🌙 22:45 • СУББОТА • СЕРВИС ЗАКРЫТ", W // 2, 140, fill=(30, 41, 59), text_col=TEXT_MUTED)

    # Main Card: Avito Messenger
    card_y = 260
    draw_card(d, [70, card_y, W - 70, card_y + 820], fill=(15, 23, 42), outline=(33, 45, 69), width=3, radius=28)
    
    # Avito App Header
    d.rounded_rectangle([70, card_y, W - 70, card_y + 90], radius=28, fill=(24, 34, 56))
    d.text((110, card_y + 26), "Авито Авто • Чат с клиентом", font=FONT_TITLE_SM, fill=TEXT_WHITE)
    d.ellipse([W - 130, card_y + 36, W - 110, card_y + 56], fill=(16, 185, 129))

    # Incoming Client Message
    msg_y = card_y + 130
    draw_card(d, [100, msg_y, W - 160, msg_y + 160], fill=(30, 41, 59), outline=(51, 65, 85), width=2, radius=18)
    d.text((125, msg_y + 25), "Клиент (Camry):", font=FONT_TITLE_SM, fill=ACCENT_BLUE)
    d.text((125, msg_y + 75), "Сколько стоит покрасить капот на Камри?\nВмятина и сколы, могу прислать фото.", font=FONT_BODY, fill=TEXT_WHITE)

    # Timer Waiting Beat
    if t > 3.5:
        progress = min(1.0, (t - 3.5) / 5.0)
        secs = int(progress * 180)
        time_str = f"0{secs // 60}:{secs % 60:02d}"
        
        tw_y = msg_y + 200
        draw_card(d, [120, tw_y, W - 120, tw_y + 130], fill=(23, 20, 35), outline=(239, 68, 68, 140), width=2, radius=18)
        d.text((150, tw_y + 22), "⏱ Время ожидания ответа:", font=FONT_BODY_SM, fill=(248, 113, 113))
        d.text((150, tw_y + 55), f"{time_str} мин  (клиент ждёт...)", font=FONT_TITLE_MD, fill=(255, 255, 255))

    # Status: Left to competitor
    if t > 7.5:
        st_y = msg_y + 360
        draw_card(d, [120, st_y, W - 120, st_y + 90], fill=(36, 18, 24), outline=WARNING_RED, width=2, radius=16)
        d.text((150, st_y + 26), "❌ Клиент ушёл к конкуренту", font=FONT_TITLE_SM, fill=WARNING_RED)

    # Dramatic Loss Callout (11s+)
    if t > 10.5:
        pulse = 0.5 + 0.5 * math.sin((t - 10.5) * 8)
        glow_alpha = int(180 + 75 * pulse)
        loss_y = 1140
        draw_card(d, [70, loss_y, W - 70, loss_y + 360], fill=(35, 12, 18), outline=(239, 68, 68, glow_alpha), width=4, radius=28)
        d.text((110, loss_y + 40), "УПУЩЕННАЯ ПРИБЫЛЬ СЕРВИСА", font=FONT_TITLE_SM, fill=(248, 113, 113))
        d.text((110, loss_y + 110), "-45 000 ₽", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 96), fill=WARNING_RED)
        d.text((110, loss_y + 245), "Пока мастера спали — клиент уехал на ремонт в другой сервис.", font=FONT_BODY_SM, fill=(254, 202, 202))

    draw_subtitle(d, sub)
    return img


def render_scene2_frame(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 3.0:
        sub = "Знакомьтесь: «Заявка 24/7»"
    elif t < 6.0:
        sub = "Виртуальный старший мастер, который не спит"
    else:
        sub = "Отвечает клиентам за 15 секунд в любое время суток!"

    # Header
    draw_badge(d, "⚡ РЕШЕНИЕ 24/7", W // 2, 140, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    # Big Solution Card
    card_y = 250
    draw_card(d, [70, card_y, W - 70, card_y + 420], fill=(13, 31, 45), outline=PRIMARY_GREEN, width=4, radius=32)
    d.text((120, card_y + 50), "Заявка 24/7", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 72), fill=TEXT_WHITE)
    d.text((120, card_y + 150), "ИИ-диспетчер и старший мастер продаж", font=FONT_TITLE_SM, fill=ACCENT_BLUE)
    
    # 15 seconds speed badge
    speed_y = card_y + 240
    draw_card(d, [120, speed_y, W - 120, speed_y + 120], fill=(16, 185, 129, 35), outline=PRIMARY_GREEN, width=2, radius=18)
    d.text((150, speed_y + 35), "⏱ Ответ за 15 секунд • 24/7", font=FONT_TITLE_MD, fill=PRIMARY_GREEN)

    # 3 Advantage Cards sliding in
    benefits = [
        ("1. Не тупой бот с кнопками", "Общается живым языком опытного мастера"),
        ("2. Оценка по фото и авто", "Распознаёт деталь и сложность ремонта"),
        ("3. Защита цен по прайсу", "Держит вилку цен строго по вашим правилам"),
    ]

    for i, (b_title, b_sub) in enumerate(benefits):
        delay = 2.5 + i * 1.5
        if t >= delay:
            by = 720 + i * 190
            draw_card(d, [70, by, W - 70, by + 160], fill=CARD_BG, outline=BORDER_COL, width=2, radius=20)
            d.text((110, by + 28), b_title, font=FONT_TITLE_SM, fill=TEXT_WHITE)
            d.text((110, by + 82), b_sub, font=FONT_BODY_SM, fill=TEXT_MUTED)

    draw_subtitle(d, sub)
    return img


def render_scene3_frame(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 3.0:
        sub = "Это не робот с кнопками."
    elif t < 6.5:
        sub = "Оценивает деталь прямо по фото"
    elif t < 10.0:
        sub = "Держит вилку цен строго по вашему прайсу"
    else:
        sub = "Забирает телефон и присылает готовую сделку в Telegram!"

    draw_badge(d, "💬 ЖИВОЙ ДИАЛОГ В АВИТО", W // 2, 110, fill=ACCENT_BLUE, text_col=(0, 0, 0))

    # Phone Frame
    phone_y = 190
    draw_card(d, [60, phone_y, W - 60, phone_y + 980], fill=(13, 20, 36), outline=(40, 56, 85), width=3, radius=32)
    
    # Phone header
    d.rounded_rectangle([60, phone_y, W - 60, phone_y + 80], radius=32, fill=(20, 30, 52))
    d.text((100, phone_y + 22), "Денис • Старший мастер (Онлайн)", font=FONT_TITLE_SM, fill=TEXT_WHITE)

    # 1. Customer Message
    c1_y = phone_y + 110
    draw_card(d, [90, c1_y, W - 180, c1_y + 140], fill=(26, 38, 62), outline=BORDER_COL, radius=18)
    d.text((115, c1_y + 20), "Клиент (Geely Monjaro):", font=FONT_TITLE_SM, fill=ACCENT_BLUE)
    d.text((115, c1_y + 68), "Оклейка зон риска на Monjaro сколько?\nПлёнка Llumar/Stek есть в наличии?", font=FONT_BODY_SM, fill=TEXT_WHITE)

    # 2. AI typing dots (3.0s - 4.5s)
    if 2.5 <= t < 4.5:
        dot_phase = int(t * 5) % 3
        dots = "." * (dot_phase + 1)
        draw_badge(d, f"Мастер печатает{dots}", 220, c1_y + 165, fill=(30, 41, 59), text_col=ACCENT_BLUE)

    # 3. AI Reply (4.5s+)
    if t >= 4.5:
        ai_y = c1_y + 160
        draw_card(d, [150, ai_y, W - 90, ai_y + 220], fill=(16, 185, 129, 25), outline=PRIMARY_GREEN, width=2, radius=18)
        d.text((175, ai_y + 18), "ИИ Старший мастер (Заявка 24/7):", font=FONT_TITLE_SM, fill=PRIMARY_GREEN)
        ai_text = (
            "Здравствуйте! На Monjaro оклейка зон риска\n"
            "полиуретаном — от 45 000 до 75 000 ₽.\n"
            "Гарантия 5 лет. На какой день записать?"
        )
        d.text((175, ai_y + 65), ai_text, font=FONT_BODY_SM, fill=TEXT_WHITE)

    # 4. Customer Phone Provided (8.5s+)
    if t >= 8.5:
        c2_y = c1_y + 405
        draw_card(d, [90, c2_y, W - 160, c2_y + 110], fill=(26, 38, 62), outline=BORDER_COL, radius=18)
        d.text((115, c2_y + 20), "Клиент:", font=FONT_TITLE_SM, fill=ACCENT_BLUE)
        d.text((115, c2_y + 60), "Да, давайте на субботу. +7 999 111-22-33", font=FONT_BODY, fill=PRIMARY_GREEN)

    # 5. Telegram Push Notification Popup (11s+)
    if t >= 11.0:
        tg_y = 1220
        draw_card(d, [60, tg_y, W - 60, tg_y + 360], fill=(15, 30, 50), outline=ACCENT_BLUE, width=4, radius=28)
        draw_badge(d, "🔔 НОВЫЙ ЛИД В TELEGRAM МАСТЕРА", W // 2, tg_y + 25, fill=ACCENT_BLUE, text_col=(0, 0, 0))
        d.text((100, tg_y + 85), "Клиент: +7 999 111-22-33", font=FONT_TITLE_MD, fill=TEXT_WHITE)
        d.text((100, tg_y + 155), "Автомобиль: Geely Monjaro (2024)", font=FONT_TITLE_SM, fill=TEXT_MUTED)
        d.text((100, tg_y + 215), "Услуга: Оклейка зон риска (45 000 – 75 000 ₽)", font=FONT_TITLE_SM, fill=PRIMARY_GREEN)
        d.text((100, tg_y + 275), "Сделка создана автоматически в CRM!", font=FONT_BODY_SM, fill=ACCENT_BLUE)

    draw_subtitle(d, sub)
    return img


def render_scene4_frame(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 3.5:
        sub = "Ответите сами — бот мгновенно уступит диалог."
    elif t < 7.5:
        sub = "Подписка окупается ровно с одного заехавшего клиента!"
    else:
        sub = "Всё остальное — ваша чистая прибыль."

    draw_badge(d, "💰 ЭКОНОМИКА И ОКУПАЕМОСТЬ", W // 2, 140, fill=GOLD_COL, text_col=(0, 0, 0))

    # Comparison Cards
    card1_y = 250
    draw_card(d, [70, card1_y, W - 70, card1_y + 280], fill=CARD_BG, outline=BORDER_COL, width=2, radius=24)
    d.text((110, card1_y + 40), "Инвестиция в сервис:", font=FONT_TITLE_SM, fill=TEXT_MUTED)
    d.text((110, card1_y + 100), "12 900 ₽ / месяц", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 68), fill=ACCENT_BLUE)
    d.text((110, card1_y + 200), "Бот работает 24/7 без выходных, отпусков и больничных", font=FONT_BODY_SM, fill=TEXT_WHITE)

    card2_y = card1_y + 320
    draw_card(d, [70, card2_y, W - 70, card2_y + 280], fill=(13, 38, 28), outline=PRIMARY_GREEN, width=3, radius=24)
    d.text((110, card2_y + 40), "1 закрытая машина на ремонт:", font=FONT_TITLE_SM, fill=PRIMARY_GREEN)
    d.text((110, card2_y + 100), "+45 000 – 75 000 ₽", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 68), fill=PRIMARY_GREEN)
    d.text((110, card2_y + 200), "Окупает подписку на 3–5 месяцев вперёд!", font=FONT_BODY_SM, fill=TEXT_WHITE)

    # Big Profit Banner (5s+)
    if t >= 4.5:
        banner_y = card2_y + 330
        draw_card(d, [70, banner_y, W - 70, banner_y + 400], fill=(20, 28, 48), outline=GOLD_COL, width=4, radius=28)
        d.text((110, banner_y + 50), "⚖️ ОКУПАЕМОСТЬ С 1 КЛИЕНТА", font=FONT_TITLE_MD, fill=GOLD_COL)
        body_p = (
            "Каждую неделю ночью и в выходные\n"
            "вы теряете 3–5 горячих клиентов.\n\n"
            "«Заявка 24/7» спасает эти заявки\n"
            "и приносит сотни тысяч чистой прибыли."
        )
        d.text((110, banner_y + 140), body_p, font=FONT_BODY, fill=TEXT_WHITE)

    draw_subtitle(d, sub)
    return img


def render_scene5_frame(t: float, dur: float) -> Image.Image:
    img = Image.new("RGBA", (W, H), BG_DARK)
    d = ImageDraw.Draw(img)

    sub = ""
    if t < 3.5:
        sub = "Тест-драйв 3 дня — бесплатно!"
    elif t < 6.5:
        sub = "Настроим под ваш прайс-лист за 10 минут."
    else:
        sub = "Забирайте горячих клиентов 24/7 прямо сейчас!"

    draw_badge(d, "🚀 НАЧНИТЕ ПРЯМО СЕЙЧАС", W // 2, 140, fill=PRIMARY_GREEN, text_col=(0, 0, 0))

    card_y = 250
    draw_card(d, [70, card_y, W - 70, card_y + 1200], fill=(13, 22, 38), outline=PRIMARY_GREEN, width=4, radius=32)

    # Logo Title
    d.text((120, card_y + 70), "Заявка 24/7", font=ImageFont.truetype(str(FONTS_DIR / "segoeuib.ttf"), 80), fill=TEXT_WHITE)
    d.text((120, card_y + 180), "ИИ-менеджер для Авито, WhatsApp и Telegram", font=FONT_TITLE_SM, fill=ACCENT_BLUE)

    # Big CTA Button
    btn_y = card_y + 280
    pulse = 0.5 + 0.5 * math.sin(t * 6)
    btn_col = (16 + int(20 * pulse), 185, 129)
    draw_card(d, [110, btn_y, W - 110, btn_y + 140], fill=btn_col, outline=(255, 255, 255), width=2, radius=20)
    d.text((160, btn_y + 42), "ТЕСТ-ДРАЙВ 3 ДНЯ БЕСПЛАТНО", font=FONT_TITLE_MD, fill=(0, 0, 0))

    # Value bullets
    bullets = [
        "✓  Настройка под ваш прайс-лист за 10 минут",
        "✓  Без обязательств и без ввода карты",
        "✓  Подключение Авито, WhatsApp и Telegram",
        "✓  Мгновенные сделки в amoCRM и Битрикс24",
    ]
    for i, b in enumerate(bullets):
        by = btn_y + 200 + i * 110
        d.text((120, by), b, font=FONT_TITLE_SM, fill=TEXT_WHITE)

    # Web Link Pill
    link_y = card_y + 920
    draw_card(d, [110, link_y, W - 110, link_y + 180], fill=(20, 32, 54), outline=ACCENT_BLUE, width=2, radius=20)
    d.text((150, link_y + 35), "🌐 Ссылка на подключение:", font=FONT_BODY_SM, fill=TEXT_MUTED)
    d.text((150, link_y + 75), "77.221.155.74.sslip.io/landing", font=FONT_TITLE_SM, fill=ACCENT_BLUE)
    d.text((150, link_y + 125), "Telegram: @Zayavka24AI_bot", font=FONT_BODY, fill=PRIMARY_GREEN)

    draw_subtitle(d, sub)
    return img


SCENE_RENDERERS = {
    "scene1": render_scene1_frame,
    "scene2": render_scene2_frame,
    "scene3": render_scene3_frame,
    "scene4": render_scene4_frame,
    "scene5": render_scene5_frame,
}


def render_scene_video(sid: str, dur: float):
    print(f"[*] Рендеринг видеоряда для {sid} (длительность {dur:.2f} с)...")
    renderer = SCENE_RENDERERS[sid]
    total_frames = int(dur * FPS)

    raw_mp4 = OUT_DIR / f"{sid}_video.mp4"
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
        str(raw_mp4)
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for i in range(total_frames):
        t = i / FPS
        frame = renderer(t, dur)
        proc.stdin.write(frame.tobytes())

    proc.stdin.close()
    proc.wait()
    print(f"[+] {sid}_video.mp4 готов ({total_frames} кадров)!")
    return raw_mp4


def mix_scene_audio(sid: str, dur: float):
    vo_wav = OUT_DIR / f"{sid}_vo.wav"
    out_audio = OUT_DIR / f"{sid}_audio.wav"

    # Add SFX overlay per scene
    if sid == "scene1":
        # Add warning sound at 10.5s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_lost.wav"),
            "-filter_complex", "[1:a]adelay=10500|10500[sfx];[0:a][sfx]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    elif sid == "scene2":
        # Add whoosh at 0s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_whoosh.wav"),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    elif sid == "scene3":
        # Add pop at 4.5s and telegram chime at 11.0s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_pop.wav"),
            "-i", str(OUT_DIR / "sfx_telegram.wav"),
            "-filter_complex", "[1:a]adelay=4500|4500[pop];[2:a]adelay=11000|11000[tg];[0:a][pop][tg]amix=inputs=3:duration=first",
            str(out_audio)
        ]
    elif sid == "scene4":
        # Add cash register at 4.5s
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_cash.wav"),
            "-filter_complex", "[1:a]adelay=4500|4500[cash];[0:a][cash]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    else:
        # Scene 5
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vo_wav),
            "-i", str(OUT_DIR / "sfx_whoosh.wav"),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first",
            str(out_audio)
        ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return out_audio


def assemble_final_video():
    scenes = ["scene1", "scene2", "scene3", "scene4", "scene5"]
    combined_scenes = []

    for sid in scenes:
        dur = get_wav_duration(OUT_DIR / f"{sid}_vo.wav")
        v_path = render_scene_video(sid, dur)
        a_path = mix_scene_audio(sid, dur)

        # Mux scene video + scene audio
        scene_muxed = OUT_DIR / f"{sid}_complete.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(v_path),
            "-i", str(a_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(scene_muxed)
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        combined_scenes.append(scene_muxed)

    # Concat file list
    concat_txt = OUT_DIR / "concat.txt"
    with open(concat_txt, "w") as f:
        for s in combined_scenes:
            f.write(f"file '{s.name}'\n")

    final_mp4 = OUT_DIR / "zayavka24_explainer_60s.mp4"
    print(f"[*] Финальная сборка ролика в {final_mp4}...")
    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_txt),
        "-c", "copy",
        str(final_mp4)
    ]
    subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print(f"\n🎉 ГОТОВО! Продающий ролик собран: {final_mp4.resolve()}")


if __name__ == "__main__":
    assemble_final_video()
