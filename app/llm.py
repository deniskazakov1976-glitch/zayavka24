import asyncio
import json
import logging
import re
from typing import Any, List, Optional

from openai import AsyncOpenAI

from app.guardrails import validate_reply
from app.models import CompanyProfile, ConversationMessage, KnowledgeGap, LLMReply, LeadInfo
from app.prompt_builder import build_system_prompt
from app.storage import Storage

logger = logging.getLogger(__name__)

# Matches Russian phone numbers like +7 (999) 123-45-67, 89991234567, 7 999 123 45 67
PHONE_REGEX = re.compile(r"(?:\+?7|8)[\s\-\(\)]*\d{3}[\s\-\(\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")

SUBMIT_LEAD_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_lead",
        "description": (
            "Вызывай эту функцию СРАЗУ, как только клиент написал свой номер телефона "
            "для записи на осмотр, расчёта или обратной связи."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "client_name": {
                    "type": "string",
                    "description": "Имя клиента, если известно",
                },
                "phone": {
                    "type": "string",
                    "description": "Номер телефона клиента в формате +7 (XXX) XXX-XX-XX",
                },
                "car_model": {
                    "type": "string",
                    "description": "Марка, модель и год автомобиля, если клиент их назвал",
                },
                "service_interest": {
                    "type": "string",
                    "description": "Какая услуга интересует клиента",
                },
                "estimated_price": {
                    "type": "string",
                    "description": "Озвученная ориентировочная стоимость, если была",
                },
                "preferred_time": {
                    "type": "string",
                    "description": "Удобный день/время для визита или звонка",
                },
                "summary": {
                    "type": "string",
                    "description": "Краткая выжимка диалога (1-2 предложения) для владельца бизнеса",
                },
            },
            "required": ["phone", "summary"],
        },
    },
}

REPORT_KNOWLEDGE_GAP_TOOL = {
    "type": "function",
    "function": {
        "name": "report_knowledge_gap",
        "description": (
            "Вызывай эту функцию, если клиент задал содержательный вопрос по услугам, "
            "ценам, графику, технологиям или материалам компании, точного ответа на который "
            "НЕТ в предоставленной базе знаний."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Суть вопроса клиента, отсутствующего в базе знаний",
                },
                "suggested_topic": {
                    "type": "string",
                    "description": "Тема вопроса (например: редкая услуга, гарантия, условия)",
                },
            },
            "required": ["question"],
        },
    },
}


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return raw.strip()


def extract_phone_fallback(text: str) -> Optional[str]:
    match = PHONE_REGEX.search(text)
    if not match:
        return None
    return normalize_phone(match.group(0))


CAR_REGEX = re.compile(
    r"(?i)\b("
    r"toyota(?:\s+camry|\s+rav4|\s+corolla|\s+prado|\s+land\s+cruiser)?|"
    r"camry|rav4|corolla|prado|"
    r"bmw(?:\s+x[1-7]|\s+[1-8]\s*сери[яи]|\s+m[3-5])?|"
    r"mercedes(?:-benz)?(?:\s+[a-z]-?class|\s+amg|\s+e\s*класс|\s+c\s*класс|\s+s\s*класс)?|"
    r"audi(?:\s+a[1-8]|\s+q[3-8])?|"
    r"volkswagen|vw(?:\s+tiguan|\s+polo|\s+passat|\s+touareg)?|"
    r"tiguan|polo|passat|touareg|"
    r"geely(?:\s+monjaro|\s+coolray|\s+atlas|\s+tugella)?|"
    r"monjaro|coolray|atlas|tugella|"
    r"haval(?:\s+jolion|\s+f7|\s+dargo|\s+h9)?|jolion|"
    r"chery(?:\s+tiggo\s*[478]?(?:\s*pro)?)?|"
    r"kia(?:\s+rio|\s+k5|\s+sportage|\s+ceed|\s+cerato)?|rio|k5|sportage|"
    r"hyundai(?:\s+solaris|\s+creta|\s+tucson|\s+santa\s+fe)?|solaris|creta|tucson|"
    r"lexus(?:\s+rx|\s+nx|\s+lx|\s+es)?|"
    r"porsche(?:\s+cayenne|\s+macan|\s+panamera)?|"
    r"skoda(?:\s+octavia|\s+rapid|\s+kodiaq)?|octavia|rapid|kodiaq|"
    r"nissan(?:\s+qashqai|\s+x-trail|\s+juke)?|qashqai|x-trail|"
    r"mazda(?:\s+[36]|\s+cx-[359])?|"
    r"lada(?:\s+vesta|\s+granta|\s+niva)?|"
    r"тойота(?:\s+камри|\s+рав4|\s+королла)?|камри|рав4|королла|"
    r"бмв(?:\s+х[1-7]|\s+[1-8]\s*сери[яи])?|"
    r"мерседес|ауди|фольксваген|тигуан|поло|"
    r"джили(?:\s+монжаро|\s+кулрей|\s+атлас)?|монжаро|кулрей|атлас|"
    r"хавал(?:\s+джолион|\s+ф7)?|джолион|"
    r"чери(?:\s+тигго)?|тигго|"
    r"киа(?:\s+рио|\s+к5|\s+спортейдж)?|рио|к5|спортейдж|"
    r"хендай|хёндай(?:\s+солярис|\s+крета|\s+туссан)?|солярис|крета|туссан|"
    r"шкода(?:\s+октавия|\s+рапид|\s+кодиак)?|октавия|рапид|кодиак|"
    r"лексус|ниссан|мазда|"
    r"лада(?:\s+веста|\s+гранта|\s+нива)?|веста|гранта|нива"
    r")\b",
    re.IGNORECASE,
)


def extract_car_model(text: str) -> Optional[str]:
    match = CAR_REGEX.search(text)
    if not match:
        return None
    val = match.group(0).strip()
    return val.title() if len(val) > 3 else val.upper()


def match_company_service(user_message: str, services: List[str]) -> Optional[str]:
    """Finds the most specific service from the company's price list matching user intent."""
    if not services:
        return None
    m = user_message.lower()

    # Bumper / body part painting
    if ("бампер" in m or "крыл" in m or "двер" in m or "капот" in m) and ("крас" in m or "покрас" in m or "маляр" in m):
        for s in services:
            s_low = s.lower()
            if "бампер" in s_low or "полная покраска" in s_low or "покраска элемента" in s_low:
                return s

    # Local painting
    if "локальн" in m and ("крас" in m or "покрас" in m):
        for s in services:
            if "локальная" in s.lower():
                return s

    # General painting
    if "крас" in m or "покрас" in m or "маляр" in m:
        for s in services:
            if "покраска" in s.lower() or "окрас" in s.lower():
                return s

    # Headlight wrapping or polishing
    if "фар" in m and ("плен" in m or "плён" in m or "оклей" in m or "бронир" in m or "полир" in m):
        for s in services:
            if "фар" in s.lower():
                return s

    # Risk zones PPF wrapping
    if ("зон" in m or "морда" in m or "передок" in m) and ("оклей" in m or "плен" in m or "плён" in m or "защит" in m):
        for s in services:
            if "зон риска" in s.lower():
                return s

    # Full PPF wrapping
    if ("полн" in m or "весь" in m or "кузов" in m or "целиком" in m) and ("оклей" in m or "плен" in m or "плён" in m):
        for s in services:
            if "полная оклейка" in s.lower():
                return s

    # General PPF wrapping
    if "оклей" in m or "плен" in m or "плён" in m or "полиуретан" in m or "винил" in m:
        for s in services:
            if "оклейка" in s.lower() or "пленк" in s.lower() or "плёнк" in s.lower():
                return s

    # Polishing
    if "полиров" in m:
        for s in services:
            if "полировка" in s.lower():
                return s

    # Ceramic coating
    if "керамик" in m or "жидк" in m:
        for s in services:
            if "керамич" in s.lower() or "покрытие" in s.lower():
                return s

    # Dry cleaning
    if "химчист" in m or "салон" in m:
        for s in services:
            if "химчистка" in s.lower():
                return s

    # Soundproofing
    if "шум" in m or "вибро" in m:
        for s in services:
            if "шумоизоляц" in s.lower():
                return s

    return None


def build_contextual_fallback(user_message: str, company: CompanyProfile) -> str:
    """Smart deterministic response when LLM is unavailable, using real profile data."""
    text_low = user_message.lower()
    car = extract_car_model(user_message)
    matched_svc = match_company_service(user_message, company.services)

    # 1. Schedule & working hours inquiry
    if any(k in text_low for k in ("воскресень", "суббот", "график", "режим", "до скольки", "со скольки", "когда работаете", "выходн", "часы")):
        schedule_text = company.work_hours or "ежедневно"
        addr_text = f" Наш бокс: {company.address}." if company.address else ""
        return (
            f"Здравствуйте! Мы работаем {schedule_text}.{addr_text} "
            f"На какое время вам удобно подъехать на осмотр к мастеру {company.manager_name}? "
            f"Напишите ваш номер телефона, чтобы забронировать удобный слот."
        )

    # 2. Discount / price objection inquiry
    if any(k in text_low for k in ("скидк", "скидку", "дешев", "дешевле", "дорого", "конкурент", "гараж", "сделайте скидку")):
        offer = company.discount_offer or "скидку 10% на первый визит"
        return (
            f"Мы держим высокое качество работ, используем профессиональные материалы и даём официальную гарантию. "
            f"Сделать большую скидку в ущерб качеству мы не можем, но для новых клиентов дарим {offer}! "
            f"Напишите ваш номер телефона — мастер {company.manager_name} закрепит за вами спецпредложение."
        )

    # 3. Warranty / guarantee inquiry
    if any(k in text_low for k in ("гаранти", "облезет", "переделаете", "брак", "отклеит")):
        addr_text = f" по адресу {company.address}" if company.address else ""
        return (
            f"Да, мы даём официальную гарантию на все виды работ и материалы! "
            f"Если возникнут любые замечания — устраняем бесплатно по гарантии. "
            f"Приезжайте к нам{addr_text} на осмотр к мастеру {company.manager_name}. "
            f"Оставьте ваш номер телефона, чтобы согласовать визит."
        )

    # 4. Specific service matched
    if matched_svc:
        car_part = f"По автомобилю {car}:\n" if car else "По вашему запросу:\n"
        addr = f" ({company.address})" if company.address else ""
        discount = f" и {company.discount_offer}" if company.discount_offer else ""
        return (
            f"Здравствуйте! {car_part}"
            f"• {matched_svc}\n\n"
            f"Точную смету мастер {company.manager_name} назовёт после быстрого осмотра детали в боксе{addr}. "
            f"Напишите ваш номер телефона — зафиксирую за вами предварительный расчёт{discount}!"
        )

    # 5. Car detected, but service not clear
    if car:
        return (
            f"Здравствуйте! Принял ваш запрос по автомобилю {car}. "
            f"Подскажите, какие именно работы планируете выполнить (покраска, оклейка плёнкой, полировка, химчистка)? "
            f"Сразу сориентирую по точной стоимости и срокам!"
        )

    # 6. Generic greeting / unclear query
    promise_tail = f" — ответит {company.media_reply_promise}" if company.media_reply_promise else ""
    return (
        f"Добрый день! На связи студия «{company.company_name}». "
        f"Подскажите, какой у вас автомобиль и что планируете сделать (покраска, оклейка, полировка)? "
        f"Сориентирую по ценам или передам мастеру {company.manager_name} в бокс{promise_tail}."
    )


class LLMService:
    """OpenAI-compatible LLM wrapper (works with ProxyAPI, VseGPT, OpenAI, AnyModel)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "gpt-4o-mini",
        fallback_models: Optional[List[str]] = None,
        temperature: float = 0.5,
        timeout: float = 25.0,
        guardrails_mode: str = "enforce",
        storage: Optional[Storage] = None,
        client: Optional[AsyncOpenAI] = None,
    ) -> None:
        self.model = model
        self.fallback_models = fallback_models or []
        self.temperature = temperature
        self.timeout = timeout
        self.guardrails_mode = guardrails_mode
        self.storage = storage
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    async def _call_llm_with_retry(self, **kwargs: Any) -> Any:
        attempts = 0
        max_retries = 1
        last_error = None
        while attempts <= max_retries:
            try:
                return await asyncio.wait_for(
                    self.client.chat.completions.create(**kwargs),
                    timeout=self.timeout,
                )
            except Exception as exc:
                last_error = exc
                err_str = str(exc)
                if any(code in err_str for code in ("401", "402", "invalid_api_key")):
                    logger.error("LLM fatal auth/billing error (no retry): %s", exc)
                    raise
                # 503 Service Unavailable: immediately raise so caller can try fallback models
                if "503" in err_str or "service_unavailable" in err_str.lower():
                    logger.warning("Model '%s' is busy (503). Fast failover...", kwargs.get("model"))
                    raise
                attempts += 1
                if attempts <= max_retries:
                    wait_time = 0.5 * (2 ** (attempts - 1))
                    logger.warning("LLM call attempt %d failed (%s). Retrying in %.1fs...", attempts, exc, wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("LLM call failed after %d retries: %s", max_retries, exc)
                    raise last_error

    async def generate_reply(
        self,
        company: CompanyProfile,
        history: List[ConversationMessage],
        user_message: str,
        channel: str = "telegram",
        chat_id: str = "demo",
        client_name_hint: str = "",
        avito_chat_url: str = "",
        image_url: Optional[str] = None,
        tenant_slug: Optional[str] = None,
    ) -> LLMReply:
        system_prompt = build_system_prompt(company)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})

        if image_url:
            content_list = []
            txt = user_message or "Оцените, пожалуйста, повреждение/состояние автомобиля по прикреплённой фотографии."
            content_list.append({"type": "text", "text": txt})
            content_list.append({"type": "image_url", "image_url": {"url": image_url}})
            messages.append({"role": "user", "content": content_list})
        else:
            messages.append({"role": "user", "content": user_message})

        candidate_models = [self.model]
        for fb in self.fallback_models:
            if fb and fb not in candidate_models:
                candidate_models.append(fb)

        response = None
        used_model = self.model

        for cand in candidate_models:
            try:
                response = await self._call_llm_with_retry(
                    model=cand,
                    temperature=self.temperature,
                    messages=messages,
                    tools=[SUBMIT_LEAD_TOOL, REPORT_KNOWLEDGE_GAP_TOOL],
                    tool_choice="auto",
                )
                if response:
                    used_model = cand
                    break
            except Exception as exc:
                logger.warning("LLM model '%s' failed for %s:%s - %s", cand, channel, chat_id, exc)
                continue

        if response is None:
            logger.error("All LLM models failed for %s:%s - falling back to contextual offline rules", channel, chat_id)
            fallback_phone = extract_phone_fallback(user_message)
            if fallback_phone:
                fallback_lead = LeadInfo(
                    channel=channel,  # type: ignore[arg-type]
                    chat_id=chat_id,
                    company_slug=company.slug,
                    client_name=client_name_hint,
                    phone=normalize_phone(fallback_phone),
                    service_interest="Запрос клиента (сохранён аварийно при сбое LLM)",
                    summary=f"Аварийный захват без LLM: {user_message[:120]}",
                    avito_chat_url=avito_chat_url,
                )
                safe_text = (
                    f"Отлично! Записал ваш номер телефона. Старший мастер {company.manager_name} свяжется с вами "
                    f"{company.media_reply_promise or 'в ближайшее время в рабочее время'} для подтверждения деталей."
                )
                return LLMReply(text=safe_text, lead=fallback_lead)

            safe_text = build_contextual_fallback(user_message, company)
            return LLMReply(text=safe_text)

        choice = response.choices[0].message
        captured_lead: Optional[LeadInfo] = None
        captured_gap: Optional[KnowledgeGap] = None

        if choice.tool_calls:
            tool_messages = list(messages)
            tool_messages.append(
                {
                    "role": "assistant",
                    "content": choice.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in choice.tool_calls
                    ],
                }
            )

            for tc in choice.tool_calls:
                if tc.function.name == "submit_lead":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    raw_phone = args.get("phone") or extract_phone_fallback(user_message) or ""
                    if raw_phone:
                        captured_lead = LeadInfo(
                            channel=channel,  # type: ignore[arg-type]
                            chat_id=chat_id,
                            company_slug=company.slug,
                            client_name=args.get("client_name") or client_name_hint,
                            phone=normalize_phone(raw_phone),
                            car_model=args.get("car_model", ""),
                            service_interest=args.get("service_interest", ""),
                            estimated_price=args.get("estimated_price", ""),
                            preferred_time=args.get("preferred_time", ""),
                            summary=args.get("summary", ""),
                            avito_chat_url=avito_chat_url,
                        )
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"status": "saved"}, ensure_ascii=False),
                        }
                    )
                elif tc.function.name == "report_knowledge_gap":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    q = args.get("question") or user_message
                    captured_gap = KnowledgeGap(
                        channel=channel,
                        chat_id=chat_id,
                        company_slug=company.slug,
                        question=q,
                        customer_message=user_message,
                        suggested_answer="",
                        status="pending",
                    )
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"status": "gap_recorded"}, ensure_ascii=False),
                        }
                    )

            try:
                followup = await self._call_llm_with_retry(
                    model=used_model,
                    temperature=self.temperature,
                    messages=tool_messages,
                )
                reply_text = (followup.choices[0].message.content or "").strip()
            except Exception:
                reply_text = "Спасибо! Записал ваши данные, передал старшему мастеру."
        else:
            reply_text = (choice.content or "").strip()

        # Regex fallback: never miss a phone number even if the model didn't call the tool
        if captured_lead is None:
            fallback_phone = extract_phone_fallback(user_message)
            if fallback_phone:
                captured_lead = LeadInfo(
                    channel=channel,  # type: ignore[arg-type]
                    chat_id=chat_id,
                    company_slug=company.slug,
                    client_name=client_name_hint,
                    phone=fallback_phone,
                    summary=f"Клиент оставил номер в сообщении: {user_message[:200]}",
                    avito_chat_url=avito_chat_url,
                )

        # Fallback for knowledge gap: if the assistant said it will check with the technician
        if captured_gap is None and ("уточню" in reply_text.lower() and ("технолог" in reply_text.lower() or "бокс" in reply_text.lower() or "детал" in reply_text.lower())):
            captured_gap = KnowledgeGap(
                channel=channel,
                chat_id=chat_id,
                company_slug=company.slug,
                question=user_message.strip(),
                customer_message=user_message.strip(),
                status="pending",
            )

        if not reply_text:
            reply_text = (
                "Спасибо! Передал ваш номер старшему мастеру — скоро свяжемся с вами "
                "и уточним все детали."
            )

        # Post-Filter Guardrails (P2)
        guard_res = validate_reply(reply_text, company, mode=self.guardrails_mode)
        if not guard_res.is_safe:
            if self.storage:
                self.storage.record_guard_event(
                    channel=channel,
                    chat_id=chat_id,
                    company_slug=company.slug,
                    original_reply=guard_res.original_text,
                    safe_reply=guard_res.text,
                    blocked_terms=", ".join(guard_res.violations),
                )
            reply_text = guard_res.text

        # Capture usage & calculate cost (Block B: TZ B1, B2)
        try:
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0

            if hasattr(response, "usage") and response.usage:
                prompt_tokens += getattr(response.usage, "prompt_tokens", 0) or 0
                completion_tokens += getattr(response.usage, "completion_tokens", 0) or 0
                total_tokens += getattr(response.usage, "total_tokens", 0) or 0

            if choice.tool_calls and 'followup' in locals() and hasattr(followup, "usage") and followup.usage:
                prompt_tokens += getattr(followup.usage, "prompt_tokens", 0) or 0
                completion_tokens += getattr(followup.usage, "completion_tokens", 0) or 0
                total_tokens += getattr(followup.usage, "total_tokens", 0) or 0

            if total_tokens == 0:
                prompt_tokens = max(1, len(user_message) // 4)
                completion_tokens = max(1, len(reply_text) // 4)
                total_tokens = prompt_tokens + completion_tokens

            cost_rub = 0.0
            if self.storage:
                p_rub, c_rub = self.storage.get_model_price(used_model)
                cost_rub = round((prompt_tokens / 1000.0) * p_rub + (completion_tokens / 1000.0) * c_rub, 4)
                t_slug = tenant_slug or getattr(company, "tenant_slug", None) or "default"
                self.storage.record_llm_usage(
                    tenant_slug=t_slug,
                    company_slug=company.slug,
                    channel=channel,
                    chat_id=chat_id,
                    model=used_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_rub=cost_rub,
                )
        except Exception as exc:
            logger.warning("Failed to record LLM usage: %s", exc)

        return LLMReply(text=reply_text, lead=captured_lead, knowledge_gap=captured_gap)

    async def analyze_image(
        self,
        image_url: str,
        prompt: str = "Кратко опиши, какое повреждение детали или состояние автомобиля видно на фото. Не выдумывай цены.",
    ) -> str:
        """Анализирует фотографию через Vision модель (P9 / TZ 9)."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]
        try:
            resp = await self._call_llm_with_retry(
                model=self.model,
                temperature=0.3,
                messages=messages,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.error("Vision analyze_image failed: %s", exc)
            return ""
