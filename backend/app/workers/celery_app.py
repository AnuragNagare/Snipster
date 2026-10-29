"""Celery application instance and configuration."""

import ssl

from celery import Celery
from celery.signals import worker_ready

from app.core.config import settings
from app.core.logging import logger



def redis_tls_options(url: str) -> dict | None:
    """
    Hosted Redis (Upstash, Redis Cloud, …) is reached over TLS: rediss://…
    Celery refuses a rediss:// URL unless it is told how to verify the certificate.
    """
    return {"ssl_cert_reqs": ssl.CERT_REQUIRED} if url.startswith("rediss://") else None


_REDIS_TLS = redis_tls_options(settings.REDIS_URL)

celery_app = Celery(
    "snipster",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,          # Only ack after task completes (safer)
    worker_prefetch_multiplier=1, # Don't prefetch — render tasks are heavy
    # NOTE: task_routes removed intentionally.
    # With a single worker started as `celery worker` (no -Q flag), the worker
    # only consumes the default "celery" queue. Routing tasks to named queues
    # (transcription / render / publish) caused them to be dispatched but never
    # picked up, leaving the video stuck at "transcribing" forever.
    # All tasks now run on the default queue. If you later scale to multiple
    # specialised workers, re-add task_routes AND launch each worker with the
    # appropriate -Q flag (e.g. -Q celery,transcription,render,publish).
    # Retry policy defaults
    task_default_retry_delay=30,  # 30 seconds between retries
    task_max_retries=3,
    broker_connection_retry_on_startup=True,
    broker_use_ssl=_REDIS_TLS,
    redis_backend_use_ssl=_REDIS_TLS,
    # The API (a serverless function) only publishes tasks. If Redis is unreachable
    # it must fail fast with an error, not hang until the function times out.
    broker_transport_options={"socket_connect_timeout": 5},
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.3,
        "interval_max": 1,
    },
)


@worker_ready.connect
def _report_worker_info(**_kwargs):
    """Tell the API what this worker can do (GPU? which encoder?) — it runs elsewhere."""
    try:
        from app.services import worker_info

        worker_info.publish()
    except Exception as e:  # reporting must never stop the worker
        logger.warning(f"[WORKER] Could not publish worker info: {e}")
