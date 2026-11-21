import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Literal, Optional, Tuple

from app.models import CompanyProfile

logger = logging.getLogger(__name__)


@dataclass
class WorkStatus:
    is_open: bool
    reason: Literal["open", "closed_hours", "day_off", "holiday", "lunch", "unknown"]
    next_open_str: str
    start_hour: int
    end_hour: int


WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAY_NAMES_RU = [
    "понедельник",
    "вторник",
    "среду",
    "четверг",
    "пятницу",
    "субботу",
    "воскресенье",
]


def parse_work_hours(work_hours_str: str) -> Tuple[int, int]:
    """Извлекает (start_hour, end_hour) из строки графика работы.

    Примеры:
    - 'с 10:00 до 21:00' -> (10, 21)
    - 'Пн–Сб с 09:00 до 20:00' -> (9, 20)
    - '10:00 - 20:00' -> (10, 20)
    - 'круглосуточно' -> (0, 24)
    """
    if not work_hours_str:
        return (9, 21)

    low = work_hours_str.lower().strip()
    if "круглосуточн" in low or "24/7" in low or "24 на 7" in low:
        return (0, 24)

    match = re.search(
        r"(?:с|c)?\s*(\d{1,2})(?::\d{2})?\s*(?:до|-|—)\s*(\d{1,2})(?::\d{2})?",
        work_hours_str,
        re.IGNORECASE,
    )
    if match:
        try:
            start_h = int(match.group(1))
            end_h = int(match.group(2))
            if 0 <= start_h <= 23 and 1 <= end_h <= 24 and start_h < end_h:
                return (start_h, end_h)
        except Exception:
            pass
    return (9, 21)


def parse_time_range(range_str: str) -> Optional[Tuple[int, int]]:
    """Парсит диапазон часов вида '14:00-15:00' или '13-14'."""
    if not range_str:
        return None
    match = re.search(
        r"(\d{1,2})(?::\d{2})?\s*(?:до|-|—)\s*(\d{1,2})(?::\d{2})?",
        range_str.strip(),
    )
    if match:
        try:
            s_h = int(match.group(1))
            e_h = int(match.group(2))
            if 0 <= s_h <= 23 and 1 <= e_h <= 24 and s_h < e_h:
                return (s_h, e_h)
        except Exception:
            pass
    return None


def get_company_datetime(
    timezone_name: str = "Europe/Moscow",
    ref_now: Optional[datetime] = None,
) -> datetime:
    """Возвращает текущее время с учётом часового пояса компании."""
    if ref_now is not None:
        return ref_now
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(timezone_name or "Europe/Moscow"))
    except Exception:
        return datetime.now()


def get_work_status(
    company: CompanyProfile,
    now: Optional[datetime] = None,
) -> WorkStatus:
    """Определяет подробный рабочий статус сервиса на текущий момент (ТЗ п. 3)."""
    dt = get_company_datetime(company.timezone, now)
    date_str = dt.strftime("%Y-%m-%d")

    # 1. Круглосуточный режим
    is_24_7 = (
        company.always_open
        or "круглосуточн" in (company.work_hours or "").lower()
        or "24/7" in (company.work_hours or "").lower()
    )
    if is_24_7:
        return WorkStatus(
            is_open=True,
            reason="open",
            next_open_str="круглосуточно",
            start_hour=0,
            end_hour=24,
        )

    # 2. Базовые рабочие часы
    start_h = company.work_start_hour
    end_h = company.work_end_hour
    if start_h is None or end_h is None:
        parsed_s, parsed_e = parse_work_hours(company.work_hours)
        start_h = start_h if start_h is not None else parsed_s
        end_h = end_h if end_h is not None else parsed_e

    # 3. Проверка праздников
    if company.holidays and date_str in company.holidays:
        return WorkStatus(
            is_open=False,
            reason="holiday",
            next_open_str="в ближайший рабочий день",
            start_hour=start_h,
            end_hour=end_h,
        )

    weekday = dt.weekday()  # 0 = Mon, 6 = Sun
    day_key_en = WEEKDAY_KEYS[weekday]
    day_key_ru = WEEKDAY_RU[weekday]

    # 4. Проверка структурного расписания
    if company.schedule:
        day_val = company.schedule.get(day_key_en) or company.schedule.get(day_key_ru)
        if isinstance(day_val, str) and (day_val.lower() in ("closed", "выходной", "off")):
            # Поиск следующего рабочего дня
            next_str = "в ближайший рабочий день"
            for step in range(1, 7):
                next_day_idx = (weekday + step) % 7
                k_en = WEEKDAY_KEYS[next_day_idx]
                k_ru = WEEKDAY_RU[next_day_idx]
                v = company.schedule.get(k_en) or company.schedule.get(k_ru)
                if v and str(v).lower() not in ("closed", "выходной", "off"):
                    parsed_next = parse_time_range(str(v))
                    next_h = parsed_next[0] if parsed_next else start_h
                    next_str = f"в {WEEKDAY_NAMES_RU[next_day_idx]} с {next_h}:00"
                    break
            return WorkStatus(
                is_open=False,
                reason="day_off",
                next_open_str=next_str,
                start_hour=start_h,
                end_hour=end_h,
            )
        elif isinstance(day_val, str):
            custom_range = parse_time_range(day_val)
            if custom_range:
                start_h, end_h = custom_range

    # 5. Проверка дней недели в текстовой строке work_hours
    wh_low = (company.work_hours or "").lower()
    if ("пн-сб" in wh_low or "пн–сб" in wh_low) and weekday == 6:
        # Воскресенье — выходной
        return WorkStatus(
            is_open=False,
            reason="day_off",
            next_open_str=f"в понедельник с {start_h}:00",
            start_hour=start_h,
            end_hour=end_h,
        )
    if ("будни" in wh_low or "пн-пт" in wh_low or "пн–пт" in wh_low) and weekday in (5, 6):
        # Выходные
        return WorkStatus(
            is_open=False,
            reason="day_off",
            next_open_str=f"в понедельник с {start_h}:00",
            start_hour=start_h,
            end_hour=end_h,
        )

    # 6. Проверка обеда
    if company.lunch_break:
        lunch_range = parse_time_range(company.lunch_break)
        if lunch_range:
            l_start, l_end = lunch_range
            if l_start <= dt.hour < l_end:
                return WorkStatus(
                    is_open=False,
                    reason="lunch",
                    next_open_str=f"после {l_end}:00",
                    start_hour=start_h,
                    end_hour=end_h,
                )

    # 7. Проверка рабочих часов
    curr_hour = dt.hour
    if start_h <= curr_hour < end_h:
        return WorkStatus(
            is_open=True,
            reason="open",
            next_open_str=f"сегодня до {end_h}:00",
            start_hour=start_h,
            end_hour=end_h,
        )

    # Вне рабочих часов (ночь/утро)
    if curr_hour >= end_h:
        next_open = f"завтра с {start_h}:00"
    else:
        next_open = f"сегодня с {start_h}:00"

    return WorkStatus(
        is_open=False,
        reason="closed_hours",
        next_open_str=next_open,
        start_hour=start_h,
        end_hour=end_h,
    )


def is_working_hours(
    company: CompanyProfile,
    now: Optional[datetime] = None,
) -> Tuple[bool, int, int]:
    """Обратная совместимость: возвращает (is_open, start_hour, end_hour)."""
    st = get_work_status(company, now)
    return st.is_open, st.start_hour, st.end_hour


def media_noun_for(media_type: str) -> str:
    """Формирует естественное русское название полученного контента."""
    mt = (media_type or "").lower().strip()
    if any(k in mt for k in ("фото", "photo", "image", "pic")):
        return "фото"
    if any(k in mt for k in ("голос", "voice", "audio", "аудио")):
        return "голосовое сообщение"
    if any(k in mt for k in ("видео", "video")):
        return "видео"
    if any(k in mt for k in ("документ", "doc", "pdf", "file")):
        return "файл"
    return "материалы"


def build_media_reply(
    company: CompanyProfile,
    media_type: str = "photo",
    has_phone: bool = False,
    now: Optional[datetime] = None,
) -> str:
    """Строит контекстно-зависимый и честный ответ клиенту при отправке медиа."""
    if company.custom_media_reply:
        return company.custom_media_reply.strip()

    status = get_work_status(company, now)
    noun = media_noun_for(media_type)
    role = company.manager_role or "старший мастер"
    name = company.manager_name or ""
    person = f"{role} {name}".strip() if name else role
    promise = company.media_reply_promise or "в течение 15–30 минут в рабочее время"

    if status.is_open:
        if company.media_reply_day:
            try:
                return company.media_reply_day.format(
                    noun=noun,
                    person=person,
                    promise=promise,
                    start_hour=status.start_hour,
                    end_hour=status.end_hour,
                )
            except Exception:
                return company.media_reply_day

        if has_phone:
            return (
                f"Спасибо, {noun} получил! Передал {person} — изучим объём работ "
                f"и свяжемся с вами по вашему номеру {promise}."
            )
        return (
            f"Спасибо, {noun} получил! Чтобы {person} оценил объём работ "
            f"и назвал точную стоимость без сюрпризов, оставьте, пожалуйста, ваш номер телефона — "
            f"посмотрим и свяжемся с вами {promise}."
        )

    # Нерабочее время: разбор причины
    if status.reason == "lunch":
        time_hint = f"сейчас у мастера обеденный перерыв, свяжемся с вами {status.next_open_str}"
    elif status.reason == "day_off":
        time_hint = f"сегодня у студии выходной день, мастер свяжется с вами {status.next_open_str}"
    elif status.reason == "holiday":
        time_hint = f"сегодня праздничный день, мастер ответит {status.next_open_str}"
    else:
        time_hint = f"сейчас мы уже не на смене, но {status.next_open_str} мастер первым делом всё изучит и сразу свяжется с вами"

    if company.media_reply_offhours:
        try:
            return company.media_reply_offhours.format(
                noun=noun,
                person=person,
                promise=promise,
                start_hour=status.start_hour,
                end_hour=status.end_hour,
            )
        except Exception:
            return company.media_reply_offhours

    if has_phone:
        return (
            f"Спасибо, {noun} получил! Передал {person}. {time_hint.capitalize()} по указанному номеру."
        )
    return (
        f"Спасибо, {noun} получил! Чтобы {person} оценил объём работ "
        f"и назвал точную стоимость без сюрпризов, оставьте, пожалуйста, ваш номер телефона. "
        f"{time_hint.capitalize()}!"
    )
