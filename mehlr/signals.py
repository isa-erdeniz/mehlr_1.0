"""
MEHLR 1.0 — Signals
Model sinyalleri — otomatik aksiyonlar.
"""
import logging
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.core.cache import cache

logger = logging.getLogger("mehlr")


@receiver(post_save, sender="mehlr.Message")
def on_message_saved(sender, instance, created, **kwargs):
    """
    Yeni mesaj kaydedilince:
    - Konuşmanın updated_at'ını güncelle
    - Proje stats cache'ini geçersiz kıl
    """
    if created:
        conv = instance.conversation
        # updated_at otomatik güncellenir (auto_now) ama yine de explicit save
        conv.save(update_fields=["updated_at"] if hasattr(conv, "updated_at") else [])

        # Cache geçersizleştir
        if conv.project:
            slug = conv.project.slug
            cache_keys = [
                f"mehlr:stats:{slug}:7d",
                f"mehlr:stats:{slug}:30d",
                "mehlr:system:stats",
            ]
            cache.delete_many(cache_keys)
            logger.debug(f"Signal: cache invalidated — {slug}")


@receiver(post_save, sender="mehlr.AnalysisReport")
def on_report_saved(sender, instance, created, **kwargs):
    """
    Yeni rapor kaydedilince:
    - Cache geçersizleştir
    - Automation module'e bildir (Celery hazırsa task tetikle)
    """
    if created:
        if instance.project:
            slug = instance.project.slug
            cache.delete_many([
                f"mehlr:stats:{slug}:7d",
                f"mehlr:stats:{slug}:30d",
            ])
            logger.info(f"Signal: new report — {slug} #{instance.id}")

        # Celery varsa bildirim task'ı tetikle
        try:
            from mehlr.tasks import send_report_notification
            send_report_notification.delay(instance.id)
        except Exception:
            pass  # Celery yoksa sessiz devam


@receiver(post_save, sender="mehlr.Project")
def on_project_saved(sender, instance, created, **kwargs):
    """
    Proje oluşturulunca veya güncellenince cache temizle.
    """
    cache.delete("mehlr:active_projects")
    if created:
        logger.info(f"Signal: new project — {instance.slug}")


@receiver(post_delete, sender="mehlr.Conversation")
def on_conversation_deleted(sender, instance, **kwargs):
    """Konuşma silinince ilgili cache geçersizleştir."""
    if instance.project:
        cache.delete_many([
            f"mehlr:stats:{instance.project.slug}:7d",
            f"mehlr:stats:{instance.project.slug}:30d",
        ])


# apps.py'de ready() içinde signals import edilmeli:
# from mehlr import signals  # noqa
