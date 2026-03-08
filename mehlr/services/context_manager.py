"""
mehlr_1.0 Context Manager — Dinamik bağlam yönetimi
"""
from mehlr.models import Project
from mehlr.prompts.project_prompts import PROJECT_PROMPTS, PROJECT_CONTEXTS


def get_enriched_context(
    project_key: str,
    conversation_history: list,
    user_message: str,
    extra_data: dict = None,
) -> str:
    """
    Proje bağlamını konuşma geçmişi ve ek veriyle zenginleştir.
    ai_engine'e geçmeden önce final system prompt için kullanılabilir.
    """
    project = PROJECT_PROMPTS.get(project_key)
    if not project:
        return PROJECT_CONTEXTS.get("general", "")

    context_parts = [
        f"Proje: {project['display_name']}",
        f"Domain: {project['domain']}",
        f"Aktif Özellikler: {', '.join(project.get('capabilities', []))}",
    ]

    if conversation_history:
        summary = _summarize_history(conversation_history)
        if summary:
            context_parts.append(f"Konuşma Özeti: {summary}")

    intent = _detect_intent(user_message, project.get("capabilities", []))
    if intent:
        context_parts.append(f"Tespit Edilen Niyet: {intent}")

    if extra_data:
        context_parts.append(f"Ek Bağlam: {extra_data}")

    return "\n".join(context_parts)


def _summarize_history(conversation_history: list) -> str:
    """Son 3 mesajdan kısa bağlam özeti üret."""
    recent = (
        conversation_history[-3:]
        if len(conversation_history) >= 3
        else conversation_history
    )
    return " | ".join(
        [
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:80]}"
            for m in recent
        ]
    )


def _detect_intent(user_message: str, capabilities: list) -> str:
    """Basit keyword → intent eşleştirme."""
    msg_lower = (user_message or "").lower()
    intent_map = {
        "rapor": "reporting",
        "analiz": "analysis",
        "öneri": "recommendation",
        "sorun": "troubleshooting",
        "karşılaştır": "comparison",
        "tahmin": "prediction",
        "liste": "listing",
        "özet": "summarization",
    }
    for keyword, intent in intent_map.items():
        if keyword in msg_lower:
            return intent
    return "general_query"


def get_project_context(project_slug=None):
    """
    Proje slug'ına göre AI bağlam metnini döndürür.
    garment_core / garment-core uyumsuzluğunu da çözer.
    """
    if not project_slug or project_slug == "general":
        return PROJECT_CONTEXTS.get("general", "")
    slug = project_slug.lower().strip()
    normalized = slug.replace("-", "_")
    ctx = PROJECT_CONTEXTS.get(slug) or PROJECT_CONTEXTS.get(normalized)
    if ctx:
        return ctx
    try:
        p = Project.objects.get(slug=slug, is_active=True)
        return p.context_prompt or PROJECT_CONTEXTS.get("general", "")
    except Project.DoesNotExist:
        try:
            p = Project.objects.get(slug=normalized, is_active=True)
            return p.context_prompt or PROJECT_CONTEXTS.get("general", "")
        except Project.DoesNotExist:
            return PROJECT_CONTEXTS.get("general", "")


def get_cross_project_context():
    """Tüm aktif projelerin özet bağlamını döndürür."""
    parts = [PROJECT_CONTEXTS.get("general", "")]
    for p in Project.objects.filter(is_active=True).order_by("name"):
        parts.append(f"- {p.name} ({p.slug}): {p.description[:100]}...")
    return "\n".join(parts)


def enrich_context(project_slug, user_query):
    """Sorguya göre bağlamı zenginleştirir (geriye dönük uyumluluk)."""
    return get_project_context(project_slug)
