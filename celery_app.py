import os
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
TIMEZONE = os.getenv("TZ", "Asia/Kolkata")

celery_app = Celery(
    "hospital_billing_scheduler",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["tasks"],
)

celery_app.conf.update(
    timezone=TIMEZONE,
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "automated-ip-billing-at-001-am": {
            "task": "tasks.run_automated_ip_billing",
            "schedule": crontab(hour=0, minute=1),
        },
    },
)
