"""Evaluate users, apply the limits and deliver (or dry-run) alerts."""
import logging
import uuid
from collections import Counter

from django.conf import settings
from django.utils import timezone

from . import channels
from .conditions import Conditions
from .engine import block_reason, channels_for, evaluate_rules
from .models import AlertLog, UserProfile
from .templates import render

logger = logging.getLogger(__name__)


def is_dry_run(override=None):
    return getattr(settings, "ALERTS_DRY_RUN", True) if override is None else override


def deliver(profile, decision, now, dry_run):
    """Write one AlertLog row per channel (and send unless dry_run). Returns the rows."""
    event_id = uuid.uuid4()
    rows = []
    for channel in decision.channels:
        msg = render(profile, decision, channel)
        row = AlertLog(profile=profile, rule=decision.rule if decision.rule.pk else None, event_id=event_id,
                       alert_type=decision.alert_type, channel=channel, severity=decision.severity,
                       language=profile.language, title=msg.title, message=msg.body,
                       aqi_value=decision.aqi, data_source=decision.source or "")
        if dry_run:
            row.status = AlertLog.Status.DRY_RUN
        else:
            try:
                row.provider_id = str(channels.send_via(channel, profile, msg) or "")[:100]
                row.status = AlertLog.Status.SENT
            except Exception as exc:  # one failing channel must never stop the others
                logger.warning("Alert via %s failed for profile %s: %s", channel, profile.pk, exc)
                row.status = AlertLog.Status.FAILED
                row.error = str(exc)[:500]
        row.save()
        rows.append(row)
    return rows


def process_profile(profile, now, dry_run, cache, summary):
    cond = Conditions(profile, now, cache)
    decisions = evaluate_rules(list(profile.rules.all()), cond)
    if not decisions:
        if cond.aqi() is None:
            summary["no_data"] += 1
        return
    limit = int(getattr(settings, "ALERTS_MAX_ALERTS_PER_RUN", 1))
    sent = 0
    for decision in decisions:
        if sent >= limit:
            summary["suppressed"]["per_run_limit"] += 1
            continue
        decision.channels = channels_for(profile, decision)
        reason = block_reason(profile, decision, now)
        if reason:
            summary["suppressed"][reason] += 1
            continue
        for row in deliver(profile, decision, now, dry_run):
            summary["logs"][str(row.status)] += 1
        summary["alerts"] += 1
        sent += 1


def evaluate_profiles(profiles=None, now=None, dry_run=None):
    """Run the whole pipeline for all active, consented users (or the given profiles)."""
    now = now or timezone.now()
    dry_run = is_dry_run(dry_run)
    if profiles is None:
        profiles = UserProfile.objects.filter(is_active=True, consent_at__isnull=False)
    summary = {"dry_run": dry_run, "profiles": 0, "alerts": 0, "no_data": 0,
               "logs": Counter(), "suppressed": Counter()}
    cache = {}
    for profile in profiles.prefetch_related("rules"):
        summary["profiles"] += 1
        try:
            process_profile(profile, now, dry_run, cache, summary)
        except Exception:
            logger.exception("Alert evaluation failed for profile %s", profile.pk)
            summary["suppressed"]["error"] += 1
    summary["logs"], summary["suppressed"] = dict(summary["logs"]), dict(summary["suppressed"])
    return summary
