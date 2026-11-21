import re
from typing import Dict, List, Tuple

# Число: 15000, 15 000, 15.000, 15 000,50, 15k / 15к
NUM = r"\d{1,3}(?:[\s\.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?[kк]?|\d{3,7}(?:[.,]\d{1,2})?"

PRICE_TAIL = re.compile(
    r"^(?P<num>" + NUM + r")\s*(?:₽|руб\.?|рублей|р\.?)?$",
    re.IGNORECASE,
)

RANGE_TAIL = re.compile(
    r"^(?P<name>.+?)\s+(?P<lo>" + NUM + r")\s*[-–—]\s*(?P<hi>" + NUM + r")\s*(?:₽|руб\.?|рублей|р\.?)?$",
    re.IGNORECASE,
)

PREFIXED_TAIL = re.compile(
    r"^(?P<name>.+?)\s+(?P<pre>от|до)\s+(?P<num>" + NUM + r")\s*(?:₽|руб\.?|рублей|р\.?)?$",
    re.IGNORECASE,
)

PLAIN_TAIL = re.compile(
    r"^(?P<name>.+?)\s+(?P<num>" + NUM + r")\s*(?:₽|руб\.?|рублей|р\.?)?$",
    re.IGNORECASE,
)

# Разделитель «название — цена».
# Дефис считается разделителем ТОЛЬКО с пробелами вокруг,
# иначе «Полировка-экспресс 5000» теряла бы часть названия.
SEPARATOR = re.compile(r"(\t+|\|+|\.{2,}|\s+[—–-]\s+|\s*[—–]\s*|\s*:\s*|\s+-\s+)")


def _format_number(raw: str) -> str:
    """Приводит число к виду «15 000» / «15 000,50»."""
    text = raw.strip().replace(" ", "").replace(".", " ").replace(" ", "")
    text = text.replace("к", "k").replace("K", "k")

    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1000
        text = text[:-1]

    text = text.replace(",", ".")
    try:
        value = float(text) * multiplier
    except ValueError:
        return raw.strip()

    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _make(name: str, price: str) -> str:
    return f"{name.strip(' \t-—–:|.')}: {price} ₽"


def parse_price_line(line: str) -> Tuple[bool, str, str]:
    """Разбирает одну строку прайс-листа.

    Возвращает (is_service, formatted_service, raw_text).
    """
    clean_line = line.strip()
    if not clean_line or clean_line.startswith("#"):
        return False, "", clean_line

    # 1. Диапазон цен: «Полировка 35 000 - 45 000»
    m = RANGE_TAIL.match(clean_line)
    if m:
        lo = _format_number(m.group("lo"))
        hi = _format_number(m.group("hi"))
        return True, _make(m.group("name"), f"{lo} – {hi}"), clean_line

    # 2. Явный разделитель: «Полировка кузова — от 15 000 ₽»
    parts = SEPARATOR.split(clean_line)
    if len(parts) >= 3:
        name = "".join(parts[:-2]).strip(" \t-—–:|.")
        tail = parts[-1].strip()
        m_price = PRICE_TAIL.match(tail)
        if m_price and name:
            return True, _make(name, _format_number(m_price.group("num"))), clean_line
        m_pref = re.match(r"^(от|до)\s+(?P<num>" + NUM + r")\s*(?:₽|руб\.?|рублей|р\.?)?$", tail, re.IGNORECASE)
        if m_pref and name:
            return True, _make(name, f"{m_pref.group(1).lower()} {_format_number(m_pref.group('num'))}"), clean_line

    # 3. «Керамика от 12 000 р» — предлог перед числом в конце строки
    m = PREFIXED_TAIL.match(clean_line)
    if m:
        return True, _make(m.group("name"), f"{m.group('pre').lower()} {_format_number(m.group('num'))}"), clean_line

    # 4. «Полировка-экспресс 5000» — дефис внутри названия не разделитель
    m = PLAIN_TAIL.match(clean_line)
    if m and len(m.group("name").strip()) >= 2:
        return True, _make(m.group("name"), _format_number(m.group("num"))), clean_line

    return False, "", clean_line


def parse_price_list(text: str) -> Dict[str, List[str]]:
    """Разбирает многострочный текст прайс-листа.

    Возвращает {"services": [...], "extra_lines": [...]}.
    """
    services: List[str] = []
    extra_lines: List[str] = []

    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        is_svc, formatted, raw = parse_price_line(trimmed)
        if is_svc:
            services.append(formatted)
        else:
            extra_lines.append(raw)

    return {
        "services": services,
        "extra_lines": extra_lines,
    }
