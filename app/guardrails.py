import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from app.models import CompanyProfile

logger = logging.getLogger(__name__)


@dataclass
class GuardResult:
    is_safe: bool
    text: str
    violations: List[str]
    original_text: str


def _extract_numbers(text: str) -> Set[int]:
    """Извлекает числовые значения из текста (например '45 000' -> 45000, '15к' -> 15000)."""
    # Remove space separators in numbers like "45 000" -> "45000"
    cleaned = re.sub(r"(\d)\s+(\d)", r"\1\2", text or "")
    numbers = set()

    # Match numbers like 45000, 1500, 10
    for match in re.finditer(r"\b(\d+)\b", cleaned):
        try:
            val = int(match.group(1))
            numbers.add(val)
        except ValueError:
            pass

    # Match abbreviations like 45к, 15 тыс (excluding mileage "150 тыс км")
    for match in re.finditer(r"(\d+)\s*(?:к|k|тыс)(?![\s\.\-]*(?:км|километр|миль))", cleaned, re.IGNORECASE):
        try:
            val = int(match.group(1)) * 1000
            numbers.add(val)
        except ValueError:
            pass

    return numbers


def extract_allowed_price_ranges(company: CompanyProfile) -> List[Tuple[int, int]]:
    """Извлекает допустимые диапазоны цен [min_p, max_p] из услуг и FAQ компании."""
    ranges: List[Tuple[int, int]] = []
    corpus = list(company.services or []) + list(company.faq or [])
    range_regex = re.compile(
        r"от\s+(\d[\d\s]*\d|\d+)\s*(?:до|-)\s*(\d[\d\s]*\d|\d+)\s*(?:₽|руб|р\b|тыс|к\b)?",
        re.IGNORECASE,
    )
    for item in corpus:
        item_clean = re.sub(r"(\d)\s+(\d)", r"\1\2", item)
        for m in range_regex.finditer(item_clean):
            try:
                mult = 1000 if any(k in m.group(0).lower() for k in ("тыс", "к")) else 1
                v1 = int(m.group(1).replace(" ", "")) * mult
                v2 = int(m.group(2).replace(" ", "")) * mult
                if v1 > v2:
                    v1, v2 = v2, v1
                ranges.append((v1, v2))
            except ValueError:
                pass
    return ranges


def is_number_allowed(num: int, allowed_numbers: Set[int], allowed_ranges: List[Tuple[int, int]]) -> bool:
    """Проверяет, разрешено ли число: есть в белом списке или попадает в вилку цен."""
    if num in allowed_numbers:
        return True
    for low, high in allowed_ranges:
        if low <= num <= high:
            return True
    return False


def extract_allowed_numbers(company: CompanyProfile) -> Set[int]:
    """Формирует белый список допустимых чисел, цен и парных сумм из базы знаний компании."""
    corpus_parts = [
        company.discount_offer or "",
        company.media_reply_promise or "",
        company.work_hours or "",
    ]
    corpus_parts.extend(company.services or [])
    corpus_parts.extend(company.advantages or [])
    corpus_parts.extend(company.faq or [])
    corpus_parts.extend(company.extra_rules or [])

    full_text = " ".join(corpus_parts)
    allowed = _extract_numbers(full_text)

    # Allow common small conversational numbers (1, 2, 3, 5, 10, 15, 20, 30, 24)
    small_numbers = {1, 2, 3, 4, 5, 7, 10, 14, 15, 20, 24, 30, 60}
    allowed.update(small_numbers)

    # Add pairwise sums of major price points to support bundled services
    price_points = [n for n in allowed if n >= 1000]
    for i in range(len(price_points)):
        for j in range(i, len(price_points)):
            pair_sum = price_points[i] + price_points[j]
            if pair_sum <= 500000:
                allowed.add(pair_sum)

    return allowed


def clean_humanize_artifacts(text: str) -> str:
    """Очищает текст от роботизированных штампов и галлюцинаций ИИ (Russian Humanizer standard)."""
    if not text:
        return ""

    res = text.strip()

    # 1. Срезаем типичные шаблонные заходы роботов поддержки
    res = re.sub(
        r"^(?:Здравствуйте[!,.]?\s*)?(?:Спасибо за обращение[!,.]?\s*)+",
        "Здравствуйте! ",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"^(?:Здравствуйте[!,.]?\s*)?(?:Принял(?:и)?\s+ваше\s+обращение[!,.]?\s*)+",
        "Здравствуйте! ",
        res,
        flags=re.IGNORECASE,
    )

    # 2. Убираем ложные заявления в прошедшем времени о названных ценах
    res = re.sub(
        r"Назвал(?:а)?\s+предварительную\s+вилку\s+цен\s*(?:по\s+прайсу)?[.!]*\s*",
        "",
        res,
        flags=re.IGNORECASE,
    )

    # 3. Чистим дублирующиеся приветствия и пробелы
    res = re.sub(r"^(?:Здравствуйте[!,.]?\s*){2,}", "Здравствуйте! ", res, flags=re.IGNORECASE)
    res = re.sub(r"[ \t]{2,}", " ", res).strip()

    # Если после зачистки в начале остался пустой восклицательный знак
    res = re.sub(r"^[\s!.,:;-]+", "", res).strip()
    return res


def validate_reply(
    reply_text: str,
    company: CompanyProfile,
    mode: str = "enforce",
) -> GuardResult:
    """Проверяет ответ бота на наличие выдуманных цен, скидок и условий.

    mode: 'off' | 'log' | 'enforce'
    """
    if mode == "off" or not reply_text:
        return GuardResult(is_safe=True, text=reply_text, violations=[], original_text=reply_text)

    cleaned_reply = clean_humanize_artifacts(reply_text)
    allowed_numbers = extract_allowed_numbers(company)
    allowed_ranges = extract_allowed_price_ranges(company)

    violations = []
    cleaned_reply_for_prices = re.sub(r"(\d)\s+(\d)", r"\1\2", cleaned_reply)

    # 1. Detect price ranges: "от 5 000 до 15 000 руб"
    range_pattern = re.compile(
        r"от\s+(\d[\d\s]*\d|\d+)\s*до\s*(\d[\d\s]*\d|\d+)\s*(?:₽|руб|р\b|тыс|к\b)",
        re.IGNORECASE,
    )
    for rm in range_pattern.finditer(cleaned_reply_for_prices):
        unit = rm.group(0).lower()
        multiplier = 1000 if any(k in unit for k in ("тыс", "к")) else 1
        for g in (1, 2):
            try:
                val = int(rm.group(g).replace(" ", "")) * multiplier
                if val > 100 and not is_number_allowed(val, allowed_numbers, allowed_ranges):
                    violations.append(f"Выдуманная цена диапазона: {rm.group(g).strip()}")
            except ValueError:
                pass

    # 2. Detect single prices or discounts (excluding mileage like "150 тыс км")
    price_pattern = re.compile(
        r"(\d[\d\s]*\d|\d+)\s*(?:₽|руб(?:лей|ля|\.|\b)?|р\b|тыс(?:\.|\b)(?![\s\.\-]*(?:км|километр|миль))|к\b(?![\s\.\-]*(?:км|километр|миль))|процент(?:ов|а)?|%)",
        re.IGNORECASE,
    )
    for m in price_pattern.finditer(cleaned_reply_for_prices):
        match_str = m.group(1)
        full_match = m.group(0).lower()
        try:
            num = int(match_str.replace(" ", ""))
            if any(k in full_match for k in ("тыс", "к")):
                num *= 1000
            if "%" in m.group(0):
                if num not in allowed_numbers:
                    violations.append(f"Выдуманная скидка: {m.group(0).strip()}")
            elif num > 100 and not is_number_allowed(num, allowed_numbers, allowed_ranges):
                violations.append(f"Выдуманная цена: {m.group(0).strip()}")
        except ValueError:
            pass

    # 3. Detect bare numbers without currency symbol after price triggers ("стоить 42000", "цена 55000")
    bare_price_pattern = re.compile(
        r"(?:стоить|стоимость|цена|обойд[её]тся|расч[её]т|составит|чек|сумма)\s*(?:в|около|примерно)?\s*(\d{4,6})\b",
        re.IGNORECASE,
    )
    for bm in bare_price_pattern.finditer(cleaned_reply_for_prices):
        try:
            val = int(bm.group(1).replace(" ", ""))
            if val > 100 and not is_number_allowed(val, allowed_numbers, allowed_ranges):
                violations.append(f"Выдуманная цена без валюты: {bm.group(1).strip()}")
        except ValueError:
            pass

    if not violations:
        return GuardResult(is_safe=True, text=cleaned_reply, violations=[], original_text=reply_text)

    logger.warning("Guardrails detected violations in company %s: %s", company.slug, violations)

    if mode == "log":
        return GuardResult(is_safe=False, text=cleaned_reply, violations=violations, original_text=reply_text)

    # Enforce mode: replace unsafe price hallucination with humanized safe lead-capture phrase
    safe_replacement = (
        f"Чтобы старший мастер точно рассчитал стоимость под ваш автомобиль без сюрпризов, "
        f"оставьте, пожалуйста, ваш номер телефона — свяжемся с вами {company.media_reply_promise or 'в рабочее время'}."
    )
    return GuardResult(is_safe=False, text=safe_replacement, violations=violations, original_text=reply_text)
