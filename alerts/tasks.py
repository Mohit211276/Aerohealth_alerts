"""Celery tasks. Names are prefixed "alerts." so they never clash with other apps' tasks."""
from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task(name="alerts.evaluate")
def evaluate():
    from .runner import evaluate_profiles
    return evaluate_profiles()


@shared_task(name="alerts.purge_logs")
def purge_logs(days=90):
    from .models import AlertLog
    return AlertLog.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()[0]
