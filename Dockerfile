FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY celery_app.py tasks.py ./

# The same image is used for the worker and Beat. Override the command
# with `celery -A celery_app:celery_app beat --loglevel=INFO` for Beat.
CMD ["celery", "-A", "celery_app:celery_app", "worker", "--loglevel=INFO"]
