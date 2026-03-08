"""
mehlr_1.0 AI Motor Servisi — Google Gemini API iletişimi.
Production-grade: chain-of-thought, fallback zinciri, güven skoru.
"""
import time
import google.generativeai as genai
from django.conf import settings
from django.core.cache import cache

from mehlr.models import Message
from mehlr.prompts.base_prompt import (
    build_system_prompt,
    build_analysis_prompt,
    MEHLR_MASTER_PROMPT,
)
from mehlr.prompts.project_prompts import PROJECT_PROMPTS
from mehlr.services.context_manager import get_project_context

# Gemini konfigürasyonu
MEHLR_CONFIG = getattr(settings, "MEHLR_CONFIG", {})

GENERATION_CONFIG = genai.types.GenerationConfig(
    temperature=MEHLR_CONFIG.get("TEMPERATURE", 0.7),
    max_output_tokens=MEHLR_CONFIG.get("MAX_TOKENS", 4096),
    top_p=0.95,
    top_k=40,
)

SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
]


def _rate_limit_key(user_id):
    return f"mehlr_rate_{user_id}"


def _check_rate_limit(user_id):
    """Dakikada RATE_LIMIT_PER_MINUTE'dan fazla istek varsa True döner."""
    limit = MEHLR_CONFIG.get("RATE_LIMIT_PER_MINUTE", 15)
    count = cache.get(_rate_limit_key(user_id), 0)
    return count >= limit


def _incr_rate_limit(user_id):
    """İstek sayacını artırır; 1 dakika TTL."""
    key = _rate_limit_key(user_id)
    count = cache.get(key, 0) + 1
    cache.set(key, count, 60)
    return count


def _cache_key(user_message, project_slug, history_hash):
    return f"mehlr_resp:{hash(user_message[:200])}:{project_slug}:{history_hash}"


def get_model(system_instruction: str, model_name: str = "gemini-1.5-pro"):
    """System instruction ile Gemini model instance'ı döner."""
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY yapılandırılmamış.")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=model_name,
        generation_config=GENERATION_CONFIG,
        safety_settings=SAFETY_SETTINGS,
        system_instruction=system_instruction,
    )


def _format_history(conversation_history: list) -> list:
    """Django Conversation/Message veya dict listesinden Gemini history formatına çevir."""
    formatted = []
    max_history = MEHLR_CONFIG.get("MAX_CONVERSATION_HISTORY", 20)
    for msg in conversation_history[-max_history:]:
        role = getattr(msg, "role", None) or msg.get("role")
        content = getattr(msg, "content", None) or msg.get("content", "")
        if role == "user":
            formatted.append({"role": "user", "parts": [content]})
        else:
            formatted.append({"role": "model", "parts": [content]})
    return formatted


def _extract_confidence(response_text: str) -> str:
    """Yanıt metninden güven skorunu parse et."""
    if not response_text:
        return "BELİRTİLMEDİ"
    if "Güven skoru: YÜKSEK" in response_text:
        return "YÜKSEK"
    if "Güven skoru: ORTA" in response_text:
        return "ORTA"
    if "Güven skoru: DÜŞÜK" in response_text:
        return "DÜŞÜK"
    return "BELİRTİLMEDİ"


def query_ai(
    project_key: str,
    user_message: str,
    conversation_history: list,
    is_analysis: bool = False,
) -> dict:
    """
    Ana AI sorgu fonksiyonu.
    Döner: {"response": str, "confidence": str, "tokens_used": int, "error": None | str}
    """
    project = PROJECT_PROMPTS.get(project_key)
    if project:
        project_system_prompt = project["system_prompt"]
    else:
        project_system_prompt = (
            MEHLR_MASTER_PROMPT
            + "\n\n## PROJE BAĞLAMI\n"
            + get_project_context(project_key or "general")
        )

    if is_analysis:
        system_instruction = build_analysis_prompt(project_system_prompt)
    else:
        system_instruction = build_system_prompt(project_system_prompt)

    try:
        model = get_model(system_instruction, model_name="gemini-1.5-pro")
        chat = model.start_chat(history=_format_history(conversation_history))
        response = chat.send_message(user_message)

        text = getattr(response, "text", "") or ""
        usage = getattr(response, "usage_metadata", None)
        tokens_used = (
            getattr(usage, "total_token_count", 0) or 0
            if usage
            else 0
        )

        return {
            "response": text,
            "confidence": _extract_confidence(text),
            "tokens_used": tokens_used,
            "error": None,
        }
    except Exception as e:
        return _fallback_query(
            project_key, user_message, conversation_history, str(e), is_analysis
        )


def _fallback_query(
    project_key: str,
    user_message: str,
    conversation_history: list,
    error_msg: str,
    is_analysis: bool = False,
) -> dict:
    """Primary model başarısız olursa gemini-1.5-flash ile tekrar dene."""
    project = PROJECT_PROMPTS.get(project_key)
    if project:
        project_system_prompt = project["system_prompt"]
    else:
        project_system_prompt = (
            MEHLR_MASTER_PROMPT
            + "\n\n## PROJE BAĞLAMI\n"
            + get_project_context(project_key or "general")
        )
    system_instruction = build_system_prompt(project_system_prompt, include_cot=False)

    try:
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            return {
                "response": "API anahtarı yapılandırılmamış.",
                "confidence": "DÜŞÜK",
                "tokens_used": 0,
                "error": "GEMINI_API_KEY yok",
            }
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=system_instruction,
        )
        chat = model.start_chat(history=_format_history(conversation_history))
        response = chat.send_message(user_message)
        text = getattr(response, "text", "") or ""
        return {
            "response": text + "\n\n_[Yedek model kullanıldı]_",
            "confidence": "ORTA",
            "tokens_used": 0,
            "error": None,
        }
    except Exception as e2:
        return {
            "response": "Şu anda yanıt üretemiyorum. Lütfen tekrar deneyin.",
            "confidence": "DÜŞÜK",
            "tokens_used": 0,
            "error": str(e2),
        }


def generate_response(user_message, conversation, project_slug=None):
    """
    Kullanıcı mesajına göre MEHLR yanıtı üretir (views ile uyumlu API).
    conversation: Conversation örneği
    project_slug: seçili proje (None ise genel)
    Döner: (response_text, tokens_used, processing_time, error_message)
    """
    from mehlr.services.query_processor import preprocess_query, rewrite_query

    user_id = conversation.user_id
    if _check_rate_limit(user_id):
        return (
            "",
            0,
            0.0,
            "Dakikada izin verilen sorgu sayısına ulaştınız. Lütfen biraz bekleyin.",
        )

    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        return (
            "",
            0,
            0.0,
            "Gemini API anahtarı yapılandırılmamış. Lütfen GEMINI_API_KEY ayarlayın.",
        )

    messages = list(
        conversation.messages.order_by("created_at")[
            -MEHLR_CONFIG.get("MAX_CONVERSATION_HISTORY", 20) :
        ]
    )
    history = [
        {
            "role": "user" if m.role == Message.Role.USER else "assistant",
            "content": m.content,
        }
        for m in messages
    ]
    history_hash = hash(tuple((h["role"], h["content"][:50]) for h in history))
    cache_ttl = MEHLR_CONFIG.get("CACHE_TTL", 300)
    project_key = project_slug or "general"
    ckey = _cache_key(user_message, project_key, history_hash)
    cached = cache.get(ckey)
    if cached:
        return cached["text"], cached.get("tokens", 0), 0.0, None

    preprocessed = preprocess_query(user_message)
    rewritten = rewrite_query(preprocessed["query"], project_key)
    is_analysis = preprocessed.get("is_analysis", False)

    start = time.time()
    result = query_ai(project_key, rewritten, history, is_analysis=is_analysis)
    elapsed = time.time() - start

    _incr_rate_limit(user_id)
    if result["error"]:
        return "", result["tokens_used"], elapsed, result["error"]
    cache.set(
        ckey,
        {"text": result["response"], "tokens": result["tokens_used"]},
        cache_ttl,
    )
    return (
        result["response"],
        result["tokens_used"],
        elapsed,
        None,
    )
