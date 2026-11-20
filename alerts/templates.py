"""Message text. Built-in defaults for English and Hindi; MessageTemplate rows override them."""
from dataclasses import dataclass

from django.conf import settings

from .personas import advice_for, category_name

DEFAULTS = {
    ("current_aqi", "en"): ("Air quality alert", "{place}: AQI is {aqi} ({category}){source_note}. {advice}"),
    ("forecast_aqi", "en"): ("Air quality forecast",
                             "AQI near {place} is expected to reach about {aqi} ({category}) in around {hours} hours. {advice}"),
    ("smoke_arrival", "en"): ("Stubble smoke alert",
                              "Smoke from crop fires may reach within {radius} km of {place} in about {hours} hours. {advice}"),
    ("stagnation", "en"): ("Pollution is trapped",
                           "{place}: AQI {aqi} ({category}) and calm, poorly mixed air, so pollution may stay high. {advice}"),
    ("current_aqi", "hi"): ("वायु गुणवत्ता चेतावनी", "{place}: AQI {aqi} ({category}){source_note}। {advice}"),
    ("forecast_aqi", "hi"): ("वायु गुणवत्ता पूर्वानुमान",
                             "{place} के पास लगभग {hours} घंटे में AQI करीब {aqi} ({category}) तक पहुँचने का अनुमान है। {advice}"),
    ("smoke_arrival", "hi"): ("पराली के धुएँ की चेतावनी",
                              "फसल अवशेष जलाने का धुआँ लगभग {hours} घंटे में {place} से {radius} किमी के भीतर पहुँच सकता है। {advice}"),
    ("stagnation", "hi"): ("प्रदूषण रुका हुआ है",
                           "{place}: AQI {aqi} ({category}) और हवा शांत है, इसलिए प्रदूषण ज़्यादा रह सकता है। {advice}"),
}
PLACE_FALLBACK = {"en": "your area", "hi": "आपका क्षेत्र"}
ESTIMATED_NOTE = {"en": " (estimated from satellite data)", "hi": " (उपग्रह डेटा से अनुमानित)"}


class _Safe(dict):
    def __missing__(self, key):
        return ""


@dataclass
class Rendered:
    title: str
    body: str


def _lookup(alert_type, language, channel):
    from .models import MessageTemplate
    row = (MessageTemplate.objects.filter(alert_type=alert_type, language=language, channel=channel, is_active=True)
           .first())
    if row:
        return row.title, row.body
    return DEFAULTS.get((alert_type, language)) or DEFAULTS[(alert_type, "en")]


def render(profile, decision, channel):
    language = profile.language if profile.language in ("en", "hi") else "en"
    title, body = _lookup(decision.alert_type, language, channel)
    values = _Safe(
        place=decision.place or PLACE_FALLBACK[language],
        aqi="" if decision.aqi is None else round(decision.aqi),
        category=category_name(decision.severity, language),
        hours=decision.hours if decision.hours is not None else "",
        radius=decision.radius_km if decision.radius_km is not None else "",
        source_note=ESTIMATED_NOTE[language] if decision.source == "satellite_model" else "",
        advice=advice_for(profile.persona, language, decision.severity),
    )
    text = body.format_map(values).strip()
    if channel == "sms":
        limit = int(getattr(settings, "ALERTS_SMS_MAX_CHARS", 300))
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
    return Rendered(title=title.format_map(values), body=text)
