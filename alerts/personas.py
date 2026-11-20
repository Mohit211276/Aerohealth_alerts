"""AQI categories, persona presets and advice text.
Thresholds and advice are product defaults, NOT medical guidance: have them reviewed by a
health professional, and the Hindi text by a native speaker, before real use."""

CATEGORY_NAMES = {
    "en": ["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"],
    "hi": ["अच्छा", "संतोषजनक", "मध्यम", "खराब", "बहुत खराब", "गंभीर"],
}
VERY_POOR = 4  # severity index from which quiet hours are ignored


def aqi_severity(aqi):
    """Indian AQI bands as an index 0..5 (Good .. Severe)."""
    for i, limit in enumerate((50, 100, 200, 300, 400)):
        if aqi <= limit:
            return i
    return 5


def category_name(severity, language="en"):
    names = CATEGORY_NAMES.get(language, CATEGORY_NAMES["en"])
    return names[max(0, min(severity, 5))]


# persona -> alert_type -> rule fields
def _r(active, thr=201, horizon=24, radius=50, cooldown=6, voice=False):
    return dict(is_active=active, aqi_threshold=thr, horizon_hours=horizon, radius_km=radius,
                cooldown_hours=cooldown, allow_voice=voice)


RULE_PRESETS = {
    "general": {"current_aqi": _r(True, 201), "forecast_aqi": _r(False, 201), "smoke_arrival": _r(True, radius=50, cooldown=12),
                "stagnation": _r(False, 201)},
    "asthma": {"current_aqi": _r(True, 101, cooldown=4), "forecast_aqi": _r(True, 101), "smoke_arrival": _r(True, radius=75, cooldown=8),
               "stagnation": _r(True, 101, cooldown=8)},
    "elderly": {"current_aqi": _r(True, 101, cooldown=4, voice=True), "forecast_aqi": _r(True, 101),
                "smoke_arrival": _r(True, radius=75, cooldown=8), "stagnation": _r(True, 101, cooldown=8)},
    "outdoor_worker": {"current_aqi": _r(True, 201), "forecast_aqi": _r(True, 201), "smoke_arrival": _r(True, radius=50, cooldown=8),
                       "stagnation": _r(False, 201)},
    "child": {"current_aqi": _r(True, 101), "forecast_aqi": _r(True, 101), "smoke_arrival": _r(True, radius=50, cooldown=8),
              "stagnation": _r(True, 101, cooldown=8)},
}

# advice per persona for three levels: moderate (<=200), poor (201-300), very poor / severe (>300)
ADVICE = {
    "en": {
        "general": ("Sensitive people should avoid long outdoor exertion.",
                    "Reduce outdoor activity and wear a mask outside.",
                    "Avoid going outside and keep windows closed."),
        "asthma": ("Carry your inhaler and limit time outdoors.",
                   "Stay indoors where possible and keep your medication handy.",
                   "Stay indoors, keep windows closed and follow your doctor's plan."),
        "elderly": ("Skip long walks and rest indoors.",
                    "Stay indoors and go out only if necessary.",
                    "Stay indoors with windows closed and call a doctor if you feel unwell."),
        "outdoor_worker": ("Take regular breaks indoors.",
                           "Take frequent indoor breaks and wear an N95 mask if you must work outside.",
                           "Postpone outdoor work if you can; wear an N95 mask if you cannot."),
        "child": ("Keep outdoor play short.",
                  "Let children play indoors today.",
                  "Keep children indoors with windows closed."),
    },
    "hi": {
        "general": ("संवेदनशील लोग लंबे समय तक बाहर मेहनत से बचें।",
                    "बाहर की गतिविधि कम करें और बाहर जाते समय मास्क पहनें।",
                    "बाहर जाने से बचें और खिड़कियाँ बंद रखें।"),
        "asthma": ("अपना इनहेलर साथ रखें और बाहर कम समय बिताएँ।",
                   "जहाँ तक हो घर के अंदर रहें और दवा पास रखें।",
                   "घर के अंदर रहें, खिड़कियाँ बंद रखें और डॉक्टर की सलाह मानें।"),
        "elderly": ("लंबी सैर टालें और घर में आराम करें।",
                    "घर के अंदर रहें और ज़रूरी हो तभी बाहर जाएँ।",
                    "खिड़कियाँ बंद करके घर में रहें और तबीयत बिगड़े तो डॉक्टर को दिखाएँ।"),
        "outdoor_worker": ("बीच-बीच में घर के अंदर आराम करें।",
                           "बार-बार अंदर विश्राम करें और बाहर काम करना ज़रूरी हो तो N95 मास्क पहनें।",
                           "संभव हो तो बाहर का काम टालें; ज़रूरी हो तो N95 मास्क पहनें।"),
        "child": ("बच्चों का बाहर खेलना कम रखें।",
                  "आज बच्चों को घर के अंदर खेलने दें।",
                  "बच्चों को खिड़कियाँ बंद करके घर में रखें।"),
    },
}


def advice_for(persona, language, severity):
    table = ADVICE.get(language, ADVICE["en"])
    tier = table.get(persona, table["general"])
    return tier[0 if severity <= 2 else 1 if severity == 3 else 2]


def build_default_rules(profile):
    """Unsaved AlertRule objects with the persona's preset values."""
    from .models import AlertRule
    preset = RULE_PRESETS.get(profile.persona, RULE_PRESETS["general"])
    return [AlertRule(profile=profile, alert_type=t, **fields) for t, fields in preset.items()]


def create_default_rules(profile):
    from .models import AlertRule
    return AlertRule.objects.bulk_create(build_default_rules(profile))
