from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    slug: str = Field(..., min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    company_name: str
    city: str
    # Мультитенантность: какой клиент (tenant) владеет этим профилем
    tenant_slug: str = "default"
    niche: str = "автобизнес"
    manager_name: str = "Александр"
    manager_role: str = "старший мастер"
    address: str = ""
    work_hours: str = ""
    discount_offer: str = "скидку 10% на первый визит"
    notify_chat_id: Optional[int] = None
    greeting: str = ""
    services: List[str] = Field(default_factory=list)
    advantages: List[str] = Field(default_factory=list)
    faq: List[str] = Field(default_factory=list)
    extra_rules: List[str] = Field(default_factory=list)
    media_reply_promise: str = "в течение 15–30 минут в рабочее время"
    media_reply_day: str = ""
    media_reply_offhours: str = ""
    custom_media_reply: str = ""
    timezone: str = "Europe/Moscow"
    work_start_hour: Optional[int] = None
    work_end_hour: Optional[int] = None

    # Structural schedule (P10 / TZ 3)
    schedule: dict[str, str] = Field(default_factory=dict)
    lunch_break: str = ""
    holidays: List[str] = Field(default_factory=list)
    always_open: bool = False

    # AI Disclosure (P8 / TZ 5)
    assistant_label: str = "виртуальный ассистент"
    disclose_ai: bool = True
    ai_disclosure_text: str = ""

    # 152-FZ Privacy (P11 / TZ 4)
    privacy_url: str = ""
    require_consent_notice: bool = True

    # CRM Integration (WO / P2)
    crm_type: Literal["none", "webhook", "amocrm", "bitrix24"] = "none"
    crm_webhook_url: str = ""
    crm_api_token: str = ""
    crm_subdomain: str = ""

    # WhatsApp Integration (WO / P3)
    whatsapp_enabled: bool = False
    whatsapp_instance_id: str = ""
    whatsapp_api_token: str = ""
    whatsapp_webhook_secret: str = ""


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: Optional[datetime] = None


class LeadInfo(BaseModel):
    channel: Literal["telegram", "avito", "whatsapp", "web"]
    chat_id: str
    company_slug: str
    client_name: str = ""
    phone: str
    car_model: str = ""
    service_interest: str = ""
    estimated_price: str = ""
    preferred_time: str = ""
    summary: str = ""
    avito_chat_url: str = ""
    created_at: Optional[datetime] = None


class KnowledgeGap(BaseModel):
    id: Optional[int] = None
    channel: str
    chat_id: str
    company_slug: str
    question: str
    customer_message: str = ""
    suggested_answer: str = ""
    status: Literal["pending", "learned", "ignored"] = "pending"
    created_at: Optional[datetime] = None


class LLMReply(BaseModel):
    text: str
    lead: Optional[LeadInfo] = None
    knowledge_gap: Optional[KnowledgeGap] = None
