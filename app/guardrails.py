import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Set

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

    # Match abbreviations like 45к, 15 тыс
    for match in re.finditer(r"(\d+)\s*(?:к|k|тыс)", cleaned, re.IGNORECASE):
        try:
            val = int(match.group(1)) * 1000
            numbers.add(val)
        except ValueError:
            pass

    return numbers


def extract_allowed_numbers(company: CompanyProfile) -> Set[int]:
    """Формирует белый список допустимых чисел и цен из базы знаний компании."""
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

    # Detect numbers that look like prices (> 500) or discounts (e.g. 50%)
    price_pattern = re.compile(
        r"(\d[\d\s]*\d|\d+)\s*(?:₽|руб|р\b|тыс|к\b|процент|%)",
        re.IGNORECASE,
    )

    violations = []
    cleaned_reply_for_prices = re.sub(r"(\d)\s+(\d)", r"\1\2", cleaned_reply)
    for m in price_pattern.finditer(cleaned_reply_for_prices):
        match_str = m.group(1)
        try:
            num = int(match_str.replace(" ", ""))
            if num > 100 and num not in allowed_numbers:
                violations.append(f"Выдуманная цена: {m.group(0).strip()}")
            elif "%" in m.group(0) and num not in allowed_numbers:
                violations.append(f"Выдуманная скидка: {m.group(0).strip()}")
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
