import asyncio
import math
import struct
import subprocess
import wave
from pathlib import Path
import edge_tts

OUT_DIR = Path("video_explainer")
OUT_DIR.mkdir(parents=True, exist_ok=True)

VOICE_PRIMARY = "ru-RU-DmitryNeural"
VOICE_FALLBACK = "ru-RU-SvetlanaNeural"

SCENES = [
    {
        "id": "scene1",
        "text": "Суббота. Половина одиннадцатого вечера. Ваш автосервис закрыт, а мастера спят. Клиент ждёт три минуты, не получает ответа и уходит к конкурентам. Вы только что потеряли сорок пять тысяч рублей.",
        "subtitles": [
            (0.0, 2.8, "Суббота. 22:30 вечера."),
            (2.8, 5.2, "Автосервис закрыт, мастера спят."),
            (5.2, 8.8, "Клиент ждёт ответа и уходит к конкурентам."),
            (8.8, 12.0, "Вы потеряли 45 000 рублей упущенной кассы."),
        ]
    },
    {
        "id": "scene2",
        "text": "Знакомьтесь: Заявка 24/7 — виртуальный старший мастер, который не спит и отвечает клиентам за 15 секунд.",
        "subtitles": [
            (0.0, 3.2, "Знакомьтесь: «Заявка 24/7»"),
            (3.2, 5.8, "Виртуальный старший мастер, который не спит"),
            (5.8, 8.5, "Отвечает клиентам на Авито за 15 секунд!"),
        ]
    },
    {
        "id": "scene3",
        "text": "Это не робот с кнопками. Он оценивает деталь прямо по фото, держит вилку цен по вашему прайсу и ведёт клиента к записи. Бот забирает номер телефона и мгновенно пересылает готовую сделку вам в Телеграм.",
        "subtitles": [
            (0.0, 2.5, "Это не примитивный бот с кнопками."),
            (2.5, 6.0, "Оценивает деталь прямо по фото"),
            (6.0, 9.2, "Держит вилку цен строго по вашему прайсу"),
            (9.2, 13.5, "Забирает телефон и присылает готовую сделку в Telegram!"),
        ]
    },
    {
        "id": "scene4",
        "text": "А если вы решите ответить сами — бот сразу уступит диалог. Подписка окупается ровно с одного заехавшего автомобиля. Всё остальное — ваша чистая прибыль.",
        "subtitles": [
            (0.0, 3.6, "Ответите сами — бот мгновенно уступит диалог."),
            (3.6, 7.8, "Окупается ровно с одного заехавшего автомобиля!"),
            (7.8, 10.5, "Всё остальное — ваша чистая прибыль."),
        ]
    },
    {
        "id": "scene5",
        "text": "Запустите тест-драйв на три дня бесплатно. Настроим под ваш прайс-лист за десять минут. Ссылка в описании.",
        "subtitles": [
            (0.0, 3.5, "Тест-драйв 3 дня — бесплатно!"),
            (3.5, 6.5, "Настроим под ваш автосервис за 10 минут."),
            (6.5, 9.0, "Забирайте клиентов 24/7 прямо сейчас!"),
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


def synth_sfx():
    sr = 44100
    print("[*] Синтез звуковых эффектов (SFX)...")

    # 1. Whoosh transition (0.35s)
    dur = 0.35
    n = int(dur * sr)
    samples = []
    for i in range(n):
        t = i / sr
        freq = 200 + 1200 * math.sin(math.pi * t / dur)
        env = math.sin(math.pi * t / dur) ** 2
        # mix sine with white noise for airy whoosh
        noise = (math.sin(i * 12.9898) * 43758.5453) % 1.0 - 0.5
        s = 0.6 * math.sin(2 * math.pi * freq * t) + 0.4 * noise
        samples.append(s * env * 0.5)
    write_wav(OUT_DIR / "sfx_whoosh.wav", samples, sr)

    # 2. Pop / Message In (0.12s)
    dur = 0.12
    n = int(dur * sr)
    samples = []
    for i in range(n):
        t = i / sr
        freq = 800 - 400 * (t / dur)
        env = math.exp(-25 * t)
        samples.append(math.sin(2 * math.pi * freq * t) * env * 0.7)
    write_wav(OUT_DIR / "sfx_pop.wav", samples, sr)

    # 3. Alert / Money Lost (0.45s)
    dur = 0.45
    n = int(dur * sr)
    samples = []
    for i in range(n):
        t = i / sr
        freq = 300 - 120 * (t / dur)
        env = math.exp(-7 * t)
        s = math.sin(2 * math.pi * freq * t) + 0.4 * math.sin(2 * math.pi * freq * 1.5 * t)
        samples.append(s * env * 0.6)
    write_wav(OUT_DIR / "sfx_lost.wav", samples, sr)

    # 4. Telegram Notification Ding (0.5s chord)
    dur = 0.5
    n = int(dur * sr)
    samples = []
    for i in range(n):
        t = i / sr
        env = math.exp(-8 * t)
        s = 0.6 * math.sin(2 * math.pi * 880 * t) + 0.4 * math.sin(2 * math.pi * 1320 * t)
        samples.append(s * env * 0.6)
    write_wav(OUT_DIR / "sfx_telegram.wav", samples, sr)

    # 5. Cash Register / Cha-ching (0.6s)
    dur = 0.6
    n = int(dur * sr)
    samples = []
    for i in range(n):
        t = i / sr
        env = math.exp(-6 * t)
        s = 0.5 * math.sin(2 * math.pi * 1046.5 * t) + 0.5 * math.sin(2 * math.pi * 1567.98 * t)
        samples.append(s * env * 0.7)
    write_wav(OUT_DIR / "sfx_cash.wav", samples, sr)
    print("[+] SFX успешно созданы!")


async def _gen_speech(text: str, mp3_path: Path):
    try:
        c = edge_tts.Communicate(text, VOICE_PRIMARY)
        await c.save(str(mp3_path))
    except Exception as e:
        print(f"[*] Dmitry ({e}) -> переключение на Svetlana...")
        c = edge_tts.Communicate(text, VOICE_FALLBACK)
        await c.save(str(mp3_path))


def generate_voiceovers():
    print("[*] Генерация озвучки через edge-tts...")
    results = {}
    for sc in SCENES:
        sid = sc["id"]
        mp3_path = OUT_DIR / f"{sid}_vo.mp3"
        wav_path = OUT_DIR / f"{sid}_vo.wav"
        
        asyncio.run(_gen_speech(sc["text"], mp3_path))

        # Convert to 44.1kHz WAV via FFmpeg
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), "-ar", "44100", "-ac", "1", str(wav_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )

        with wave.open(str(wav_path), "r") as wf:
            dur = wf.getnframes() / float(wf.getframerate())
        results[sid] = round(dur, 2)
        print(f"[+] {sid}: {dur:.2f} сек")

    return results


if __name__ == "__main__":
    synth_sfx()
    durs = generate_voiceovers()
    print("Длительности сцен:", durs)
    total = sum(durs.values())
    print(f"Общая длительность чистого голоса: {total:.1f} сек")
