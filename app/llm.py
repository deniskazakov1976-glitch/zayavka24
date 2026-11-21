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


class LLMService:
    """OpenAI-compatible LLM wrapper (works with ProxyAPI, VseGPT, OpenAI)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.5,
        timeout: float = 25.0,
        guardrails_mode: str = "enforce",
        storage: Optional[Storage] = None,
        client: Optional[AsyncOpenAI] = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.guardrails_mode = guardrails_mode
        self.storage = storage
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def _call_llm_with_retry(self, **kwargs: Any) -> Any:
        attempts = 0
        max_retries = 2
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

        try:
            response = await self._call_llm_with_retry(
                model=self.model,
                temperature=self.temperature,
                messages=messages,
                tools=[SUBMIT_LEAD_TOOL, REPORT_KNOWLEDGE_GAP_TOOL],
                tool_choice="auto",
            )
        except Exception as exc:
            logger.error("LLM service unavailable for %s:%s - %s", channel, chat_id, exc)
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
                    "Спасибо! Записал ваш номер телефона. Старший мастер свяжется с вами "
                    f"{company.media_reply_promise or 'в ближайшее время в рабочее время'} для подтверждения деталей."
                )
                return LLMReply(text=safe_text, lead=fallback_lead)

            safe_text = (
                "Здравствуйте! Принял ваше обращение. Чтобы старший мастер мог сориентировать вас "
                f"по точной стоимости и времени, оставьте, пожалуйста, ваш номер телефона — свяжемся с вами "
                f"{company.media_reply_promise or 'в ближайшее время в рабочее время'}."
            )
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
                    model=self.model,
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
            used_model = self.model

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
                used_model = "unknown-usage"

            cost_rub = 0.0
            if self.storage:
                p_rub, c_rub = self.storage.get_model_price(self.model)
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
