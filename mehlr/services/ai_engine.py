"""
mehlr_1.0 AI Motor Servisi — Google Gemini API (google-genai SDK).
"""
import time
import logging
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("mehlr")

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    genai = None
    types = None
    logger.warning("google-genai paketi bulunamadı.")

MEHLR_CONFIG = getattr(settings, "MEHLR_CONFIG", {})
PRIMARY_MODEL = MEHLR_CONFIG.get("PRIMARY_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = MEHLR_CONFIG.get("FALLBACK_MODEL", "gemini-2.5-flash")
MAX_TOKENS = MEHLR_CONFIG.get("MAX_TOKENS", 4096)
TEMPERATURE = MEHLR_CONFIG.get("TEMPERATURE", 0.7)
CACHE_TTL = MEHLR_CONFIG.get("CACHE_TTL", 300)
RATE_LIMIT = MEHLR_CONFIG.get("RATE_LIMIT_PER_MINUTE", 15)
MAX_HISTORY = MEHLR_CONFIG.get("MAX_CONVERSATION_HISTORY", 20)


def _get_client():
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY tanımlı değil.")
    http_options = types.HttpOptions(timeout=30_000)  # 30 saniye (ms)
    return genai.Client(api_key=api_key, http_options=http_options)


def query_ai(project_key, user_message, conversation_history=None,
             is_analysis=False, system_prompt=None):
    """
    Gemini'ye istek gönderir.
    Döner: {"response": str, "confidence": str, "tokens_used": int, "error": str|None}
    """
    if not GENAI_AVAILABLE:
        return {"response": "", "confidence": "DÜŞÜK", "tokens_used": 0,
                "error": "google-genai paketi yüklü değil."}

    from mehlr.prompts.base_prompt import build_system_prompt, build_analysis_prompt
    from mehlr.prompts.project_prompts import PROJECT_PROMPTS

    meta = PROJECT_PROMPTS.get(project_key, {})
    project_system = meta.get("system_prompt", "")

    if system_prompt:
        full_system = system_prompt
    elif is_analysis:
        full_system = build_analysis_prompt(project_system)
    else:
        full_system = build_system_prompt(project_system)

    # Geçmiş mesajları formatla
    history = conversation_history or []
    contents = []
    for msg in list(history)[-MAX_HISTORY:]:
        role = "user" if msg.get("role") == "user" else "model"
        contents.append(types.Content(
            role=role,
            parts=[types.Part(text=msg.get("content", ""))]
        ))
    contents.append(types.Content(
        role="user",
        parts=[types.Part(text=user_message)]
    ))

    config = types.GenerateContentConfig(
        system_instruction=full_system,
        max_output_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
    )

    try:
        client = _get_client()
        response = client.models.generate_content(
            model=PRIMARY_MODEL,
            contents=contents,
            config=config,
        )
        text = response.text or ""
        tokens = getattr(response.usage_metadata, "total_token_count", 0) or 0
        confidence = _extract_confidence(text)
        return {"response": text, "confidence": confidence,
                "tokens_used": tokens, "error": None}

    except Exception as e:
        err_str = str(e).lower()
        if "timeout" in err_str or "deadline" in err_str:
            logger.warning(f"query_ai timeout: {e}")
            return {
                "response": "Yanıt süresi aşıldı, lütfen tekrar deneyin.",
                "confidence": "DÜŞÜK",
                "tokens_used": 0,
                "error": "timeout",
            }
        logger.error(f"query_ai primary failed: {e}")
        return _fallback_query(contents, config, str(e))


def _fallback_query(contents, config, original_error):
    try:
        client = _get_client()
        fallback_config = types.GenerateContentConfig(
            system_instruction=getattr(config, "system_instruction", None),
            max_output_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        response = client.models.generate_content(
            model=FALLBACK_MODEL,
            contents=contents,
            config=fallback_config,
        )
        text = response.text or ""
        tokens = getattr(response.usage_metadata, "total_token_count", 0) or 0
        logger.info("Fallback model kullanıldı.")
        return {"response": text, "confidence": "ORTA",
                "tokens_used": tokens, "error": None}
    except Exception as e:
        err_str = str(e).lower()
        if "timeout" in err_str or "deadline" in err_str:
            return {"response": "Yanıt süresi aşıldı, lütfen tekrar deneyin.",
                    "confidence": "DÜŞÜK", "tokens_used": 0,
                    "error": "timeout"}
        logger.error(f"Fallback da başarısız: {e}")
        return {"response": "Şu an yanıt üretemiyorum, lütfen tekrar deneyin.",
                "confidence": "DÜŞÜK", "tokens_used": 0,
                "error": str(e)}


def _extract_confidence(text):
    text_upper = text.upper()
    if "YÜKSEK" in text_upper or "HIGH" in text_upper:
        return "YÜKSEK"
    if "ORTA" in text_upper or "MEDIUM" in text_upper:
        return "ORTA"
    if "DÜŞÜK" in text_upper or "LOW" in text_upper:
        return "DÜŞÜK"
    return "ORTA"


def generate_response(user_message, conversation, project_slug=None):
    """
    Eski API — views.py uyumluluğu için korundu.
    Döner: (response_text, tokens_used, elapsed, error)
    """
    start = time.time()

    # Rate limit cache kontrolü (kullanıcı bazlı)
    user_id = getattr(conversation, "user_id", None) or "anon"
    cache_key = f"mehlr:rate:{user_id}"
    count = cache.get(cache_key, 0)
    if count >= RATE_LIMIT:
        return ("Dakikada izin verilen sorgu sayısına ulaştınız. Lütfen bekleyin.", 0, 0.0, "rate_limit")
    cache.set(cache_key, count + 1, timeout=60)

    # Konuşma geçmişi
    if conversation:
        msgs = list(conversation.messages.order_by("created_at"))[-MAX_HISTORY:]
        history = [{"role": m.role, "content": m.content} for m in msgs]
        # Son mesaj user ise (views'da az önce eklenen), history'den çıkar
        # çünkü query_ai zaten user_message'ı sonuna ekliyor
        if history and history[-1]["role"] == "user":
            history = history[:-1]
    else:
        history = []

    result = query_ai(
        project_key=project_slug or "general",
        user_message=user_message,
        conversation_history=history,
    )

    elapsed = round(time.time() - start, 2)
    return (
        result["response"],
        result["tokens_used"],
        elapsed,
        result["error"],
    )
