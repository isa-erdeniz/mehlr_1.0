"""
MEHLR 1.0 — Celery Tasks
Broker bağlantısı olmadan iskelet olarak çalışır.
CELERY_BROKER_URL tanımlanınca otomatik aktif olur.
"""
import logging
from datetime import timedelta
from django.utils import timezone

logger = logging.getLogger("mehlr")

# Celery yoksa sessizce degrade et
try:
    from celery import shared_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    def shared_task(*args, **kwargs):
        """Celery yoksa decorator olarak çalışır ama task kaydetmez."""
        def decorator(fn):
            return fn
        return decorator if args and callable(args[0]) else decorator


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def generate_scheduled_report(self, project_slug: str, report_type: str = "summary"):
    """
    Zamanlanmış rapor üretimi.
    Celery beat ile periyodik çalıştırılır.
    """
    try:
        from mehlr.models import Project
        from mehlr.services.report_generator import generate_report

        project = Project.objects.get(slug=project_slug, is_active=True)
        report = generate_report(project=project, report_type=report_type)
        logger.info(f"Task: scheduled report generated — {project_slug} #{report.id}")
        return {"status": "ok", "report_id": report.id}

    except Exception as exc:
        logger.error(f"Task: generate_scheduled_report failed — {project_slug}: {exc}")
        if CELERY_AVAILABLE:
            raise self.retry(exc=exc)
        return {"status": "error", "error": str(exc)}


@shared_task
def cleanup_old_conversations(days: int = 90):
    """
    N günden eski boş konuşmaları temizler.
    """
    from mehlr.models import Conversation

    cutoff = timezone.now() - timedelta(days=days)
    old_empty = Conversation.objects.filter(
        created_at__lt=cutoff,
        messages__isnull=True,
    ).distinct()
    count = old_empty.count()
    old_empty.delete()
    logger.info(f"Task: cleanup_old_conversations — {count} deleted (>{days}d)")
    return {"deleted": count}


@shared_task
def send_report_notification(report_id: int, user_id: int = None):
    """
    Rapor hazır olduğunda kullanıcıya bildirim gönderir.
    E-posta/webhook entegrasyonu için yer tutucu.
    """
    logger.info(f"Task: send_report_notification — report #{report_id}, user #{user_id}")
    # TODO: E-posta veya webhook entegrasyonu ekle
    return {"status": "queued", "report_id": report_id}


@shared_task
def refresh_project_stats_cache(project_slug: str = None):
    """
    Proje analitik cache'ini yeniler.
    Dashboard hızı için kullanılır.
    """
    from django.core.cache import cache
    from mehlr.modules.analytics import AnalyticsModule
    from mehlr.models import Project
    from mehlr.prompts.project_prompts import PROJECT_PROMPTS

    analytics = AnalyticsModule()
    slugs = [project_slug] if project_slug else list(PROJECT_PROMPTS.keys())

    for slug in slugs:
        try:
            stats = analytics.get_project_stats(slug, days=30)
            cache_key = f"mehlr:stats:{slug}:30d"
            cache.set(cache_key, stats, timeout=3600)
            logger.debug(f"Task: cache refreshed — {slug}")
        except Exception as e:
            logger.warning(f"Task: cache refresh failed — {slug}: {e}")

    return {"refreshed": slugs}
