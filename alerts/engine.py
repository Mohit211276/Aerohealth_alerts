"""Decide WHAT to alert (rules) and WHETHER it may be sent (quiet hours, daily cap, cooldown)."""
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count

from .models import AlertLog
from .personas import VERY_POOR, aqi_severity

TYPE_ORDER = ["current_aqi", "smoke_arrival", "forecast_aqi", "stagnation"]
FAILED_BACKOFF = timedelta(minutes=30)


@dataclass
class Decision:
    rule: object
    alert_type: str
    severity: int
    aqi: Optional[float] = None
    hours: Optional[int] = None
    radius_km: Optional[int] = None
    source: Optional[str] = None
    place: str = ""
    channels: List[str] = field(default_factory=list)


# ---- rules -----------------------------------------------------------------

def _current(rule, cond):
    a = cond.aqi()
    if a and a["value"] >= rule.aqi_threshold:
        return Decision(rule, rule.alert_type, aqi_severity(a["value"]), aqi=a["value"],
                        source=a["source"], place=a["place"])


def _forecast(rule, cond):
    a = cond.aqi()
    if a and a["value"] >= rule.aqi_threshold:
        return None  # already above the threshold: the current-AQI alert covers it
    hits = [r for r in cond.forecast() if r["hours"] <= rule.horizon_hours and r["prediction"] >= rule.aqi_threshold]
    if not hits:
        return None
    peak = max(hits, key=lambda r: r["prediction"])
    return Decision(rule, rule.alert_type, aqi_severity(peak["prediction"]), aqi=peak["prediction"],
                    hours=hits[0]["hours"], source="forecast", place=hits[0]["place"])


def _smoke(rule, cond):
    s = cond.smoke(rule.radius_km)
    if s and s["hours"] <= rule.horizon_hours:
        severity = 3 if s["hours"] <= 6 else 2
        return Decision(rule, rule.alert_type, severity, hours=s["hours"], radius_km=rule.radius_km,
                        source="smoke")


def _stagnation(rule, cond):
    a, d = cond.aqi(), cond.dispersion()
    if a and d and d["poor"] and a["value"] >= rule.aqi_threshold:
        return Decision(rule, rule.alert_type, aqi_severity(a["value"]), aqi=a["value"],
                        source=a["source"], place=a["place"])


RULES = {"current_aqi": _current, "forecast_aqi": _forecast, "smoke_arrival": _smoke, "stagnation": _stagnation}


def evaluate_rules(rules, cond):
    """All active rules that fire for these conditions, most important first."""
    out = []
    for rule in rules:
        if rule.is_active and rule.alert_type in RULES:
            decision = RULES[rule.alert_type](rule, cond)
            if decision:
                out.append(decision)
    out.sort(key=lambda d: (-d.severity, TYPE_ORDER.index(d.alert_type)))
    return out


# ---- limits ----------------------------------------------------------------

def phone_ok(profile):
    """SMS and voice need a verified phone (switch off with ALERTS_REQUIRE_VERIFIED_PHONE = False)."""
    required = getattr(settings, "ALERTS_REQUIRE_VERIFIED_PHONE", True)
    return bool(profile.phone) and (bool(profile.phone_verified_at) or not required)


def channels_for(profile, decision):
    channels = []
    if profile.push_enabled and profile.fcm_token:
        channels.append("push")
    if profile.sms_enabled and phone_ok(profile):
        channels.append("sms")
    rule = decision.rule
    if (profile.voice_enabled and phone_ok(profile) and rule.allow_voice and decision.aqi is not None
            and decision.aqi >= rule.voice_min_aqi):
        channels.append("voice")
    return channels


def _local(profile, now):
    try:
        return now.astimezone(ZoneInfo(profile.timezone))
    except Exception:
        return now.astimezone(ZoneInfo("Asia/Kolkata"))


def in_quiet_hours(profile, now):
    if not (profile.quiet_start and profile.quiet_end) or profile.quiet_start == profile.quiet_end:
        return False
    t = _local(profile, now).time()
    if profile.quiet_start < profile.quiet_end:
        return profile.quiet_start <= t < profile.quiet_end
    return t >= profile.quiet_start or t < profile.quiet_end


def block_reason(profile, decision, now):
    """None if the alert may be sent now, otherwise a short reason code."""
    if not profile.is_active:
        return "inactive"
    if not profile.consent_at:
        return "no_consent"
    if not decision.channels:
        return "no_channel"
    if in_quiet_hours(profile, now) and decision.severity < VERY_POOR:
        return "quiet_hours"

    logs = AlertLog.objects.filter(profile=profile)
    day_start = _local(profile, now).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = (logs.filter(status__in=["sent", "dry_run"], created_at__gte=day_start)
                  .aggregate(n=Count("event_id", distinct=True))["n"])
    if sent_today >= profile.max_alerts_per_day:
        return "daily_cap"

    same_type = logs.filter(alert_type=decision.alert_type)
    last = same_type.filter(status__in=["sent", "dry_run"]).first()
    if last and now - last.created_at < timedelta(hours=decision.rule.cooldown_hours) \
            and decision.severity <= last.severity:
        return "cooldown"          # a worse category (escalation) is allowed through
    if same_type.filter(status="failed", created_at__gte=now - FAILED_BACKOFF).exists():
        return "retry_backoff"
    return None
