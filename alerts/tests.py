from datetime import datetime, time, timedelta, timezone as dt_tz
from io import StringIO
from unittest import mock, skipUnless
from urllib.parse import urlencode

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import include, path
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.views import APIView

from . import channels, runner, verification
from . import urls as alerts_urls
from .conditions import Conditions, _model
from .engine import Decision
from .models import AlertLog, AlertRule, MessageTemplate, UserProfile
from .personas import (advice_for, aqi_severity, build_default_rules, category_name, create_default_rules)
from .templates import render
from .views import twilio_signature

urlpatterns = [path("api/alerts/", include(alerts_urls))]
HAS_WEATHER = _model("weather", "AirQualityForecast") is not None


def mk_profile(**kw):
    values = dict(name="Asha", phone="+919876543210", fcm_token="tok", language="en", persona="general",
                  lat=28.65, lon=77.32, push_enabled=True, consent_at=timezone.now(),
                  phone_verified_at=timezone.now())
    values.update(kw)
    profile = UserProfile.objects.create(**values)
    create_default_rules(profile)
    return profile


class Fake:
    """A stand-in channel sender."""
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def send(self, profile, msg):
        if self.fail:
            raise channels.SendError(self.fail)
        self.calls.append(msg.body)
        return f"fake-{len(self.calls)}"


class PersonaTests(SimpleTestCase):
    def test_severity_bands(self):
        self.assertEqual([aqi_severity(v) for v in (0, 50, 51, 100, 101, 200, 201, 300, 301, 400, 401, 500)],
                         [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5])

    def test_category_names_in_two_languages(self):
        self.assertEqual(category_name(3, "en"), "Poor")
        self.assertEqual(category_name(3, "hi"), "खराब")
        self.assertEqual(category_name(9, "en"), "Severe")

    def test_advice_tiers_and_fallback(self):
        self.assertIn("inhaler", advice_for("asthma", "en", 2))
        self.assertIn("doctor", advice_for("asthma", "en", 4))
        self.assertEqual(advice_for("asthma", "xx", 3), advice_for("asthma", "en", 3))

    def test_presets_differ_by_persona(self):
        def rules(persona):
            return {r.alert_type: r for r in build_default_rules(UserProfile(persona=persona))}
        self.assertEqual(len(rules("general")), 4)
        self.assertEqual(rules("general")["current_aqi"].aqi_threshold, 201)
        self.assertEqual(rules("asthma")["current_aqi"].aqi_threshold, 101)
        self.assertTrue(rules("elderly")["current_aqi"].allow_voice)
        self.assertFalse(rules("general")["forecast_aqi"].is_active)


class TemplateTests(TestCase):
    def decision(self, **kw):
        values = dict(rule=None, alert_type="current_aqi", severity=3, aqi=250.4, place="Anand Vihar")
        values.update(kw)
        return Decision(**values)

    def test_english_and_hindi(self):
        en = render(UserProfile(persona="general", language="en"), self.decision(), "push")
        self.assertEqual(en.title, "Air quality alert")
        self.assertIn("AQI is 250 (Poor)", en.body)
        self.assertIn("mask", en.body)
        hi = render(UserProfile(persona="general", language="hi"), self.decision(), "push")
        self.assertIn("खराब", hi.body)
        self.assertIn("Anand Vihar", hi.body)

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(render(UserProfile(persona="general", language="ta"), self.decision(), "push").title,
                         "Air quality alert")

    def test_satellite_estimate_is_labelled_and_place_has_a_fallback(self):
        d = self.decision(source="satellite_model", place="")
        body = render(UserProfile(persona="general", language="en"), d, "push").body
        self.assertIn("your area", body)
        self.assertIn("estimated from satellite data", body)

    def test_forecast_and_smoke_wording(self):
        p = UserProfile(persona="asthma", language="en")
        f = render(p, self.decision(alert_type="forecast_aqi", hours=24, source="forecast"), "push").body
        self.assertIn("in around 24 hours", f)
        s = render(p, self.decision(alert_type="smoke_arrival", aqi=None, severity=3, hours=6, radius_km=50), "push").body
        self.assertIn("within 50 km", s)

    def test_database_template_overrides_default_and_bad_placeholders_are_blank(self):
        MessageTemplate.objects.create(alert_type="current_aqi", language="en", channel="sms",
                                       title="T", body="AQI {aqi} {nonsense}!")
        sms = render(UserProfile(persona="general", language="en"), self.decision(), "sms")
        self.assertEqual(sms.body, "AQI 250 !")
        push = render(UserProfile(persona="general", language="en"), self.decision(), "push")
        self.assertEqual(push.title, "Air quality alert")   # other channels keep the default

    @override_settings(ALERTS_SMS_MAX_CHARS=40)
    def test_sms_is_trimmed(self):
        sms = render(UserProfile(persona="general", language="en"), self.decision(), "sms")
        self.assertLessEqual(len(sms.body), 40)
        self.assertTrue(sms.body.endswith("…"))


class BaseData(TestCase):
    """A CPCB station at Anand Vihar plus helpers to add readings, forecasts and smoke."""

    def setUp(self):
        self.Location = apps.get_model("airquality", "Location")
        self.station = self.Location.objects.create(name="Anand Vihar", state="Delhi", lat=28.65, lon=77.32,
                                                    is_cpcb_station=True)
        self.now = timezone.now()

    def reading(self, aqi, hours_ago=1.0, at=None):
        GroundReading = apps.get_model("airquality", "GroundReading")
        base = at or self.now
        return GroundReading.objects.create(location=self.station, timestamp=base - timedelta(hours=hours_ago), aqi=aqi)

    def run_for(self, profile, now=None, dry_run=True):
        return runner.evaluate_profiles(profiles=UserProfile.objects.filter(pk=profile.pk),
                                        now=now or self.now, dry_run=dry_run)

    def forecast(self, prediction, hours=24, version="lgbm-v1", run_at=None):
        Forecast = apps.get_model("weather", "AirQualityForecast")
        return Forecast.objects.create(location=self.station, prediction=prediction, model_version=version,
                                       forecast_for=self.now + timedelta(hours=hours), horizon_hours=hours,
                                       run_at=run_at or self.now)

    def smoke(self, hours=6, lat=28.70, lon=77.30):
        Fire = apps.get_model("airquality", "FireHotspot")
        Drift = apps.get_model("weather", "SmokeDriftPrediction")
        fire = Fire.objects.create(lat=30.9, lon=75.8, acq_datetime=self.now, frp=12.0, confidence="h")
        return Drift.objects.create(hotspot=fire, hours_ahead=hours, predicted_for=self.now + timedelta(hours=hours),
                                    lat=lat, lon=lon, distance_km=100, wind_speed_ms=5, wind_direction_deg=270)

    def weather(self, wind=2.0, blh=1000.0):
        Reading = apps.get_model("weather", "WeatherReading")
        return Reading.objects.create(location=self.station, timestamp=self.now, wind_speed_ms=wind,
                                      boundary_layer_m=blh, wind_direction_deg=270)


class ConditionsTests(BaseData):
    def test_station_reading_is_used(self):
        self.reading(250)
        a = Conditions(mk_profile(), self.now).aqi()
        self.assertEqual((a["value"], a["source"], a["place"]), (250.0, "cpcb_station", "Anand Vihar"))

    def test_stale_reading_is_ignored(self):
        self.reading(250, hours_ago=7)
        self.assertIsNone(Conditions(mk_profile(), self.now).aqi())

    def test_satellite_grid_is_the_fallback(self):
        apps.get_model("airquality", "AQIGridPoint").objects.create(
            lat=28.66, lon=77.33, timestamp=self.now - timedelta(hours=1), aqi=260, model_version="x")
        a = Conditions(mk_profile(lat=28.70, lon=77.40), self.now).aqi()
        self.assertEqual((a["value"], a["source"]), (260.0, "satellite_model"))

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_forecast_uses_newest_run_and_skips_persistence(self):
        self.forecast(300, run_at=self.now - timedelta(hours=3))      # older run
        self.forecast(180)                                            # newest run
        self.forecast(250, hours=48, version="persistence")           # demo rows are ignored
        rows = Conditions(mk_profile(), self.now).forecast()
        self.assertEqual([r["prediction"] for r in rows], [180.0])
        self.assertEqual(rows[0]["hours"], 24)

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_old_forecast_run_is_stale(self):
        self.forecast(180, run_at=self.now - timedelta(hours=30))
        self.assertEqual(Conditions(mk_profile(), self.now).forecast(), [])

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_smoke_and_dispersion(self):
        self.smoke(hours=6)
        self.weather(wind=2.0, blh=1000.0)
        c = Conditions(mk_profile(), self.now)
        self.assertEqual(c.smoke(50)["hours"], 6)
        self.assertIsNone(Conditions(mk_profile(lat=12.0, lon=78.0), self.now).smoke(50))
        self.assertEqual(c.dispersion(), {"ventilation_index": 2000.0, "poor": True})


class PipelineTests(BaseData):
    def logs(self, profile):
        return list(AlertLog.objects.filter(profile=profile).order_by("id"))

    def test_current_alert_in_dry_run(self):
        self.reading(250)
        p = mk_profile()
        s = self.run_for(p)
        self.assertEqual((s["alerts"], s["logs"]), (1, {"dry_run": 1}))
        log = self.logs(p)[0]
        self.assertEqual((log.status, log.channel, log.alert_type, log.severity), ("dry_run", "push", "current_aqi", 3))
        self.assertIn("250", log.message)

    def test_default_mode_is_dry_run_so_nothing_is_ever_sent_by_accident(self):
        self.reading(250)
        p = mk_profile()
        fake = Fake()
        with mock.patch.dict(channels.SENDERS, {"push": fake}):
            runner.evaluate_profiles(profiles=UserProfile.objects.filter(pk=p.pk), now=self.now)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.logs(p)[0].status, "dry_run")

    def test_threshold_depends_on_persona(self):
        self.reading(150)
        self.assertEqual(self.run_for(mk_profile(persona="general"))["alerts"], 0)
        self.assertEqual(self.run_for(mk_profile(persona="asthma", name="B"))["alerts"], 1)

    def test_stale_data_gives_no_alert_and_is_reported(self):
        self.reading(400, hours_ago=7)
        s = self.run_for(mk_profile())
        self.assertEqual((s["alerts"], s["no_data"]), (0, 1))

    def test_satellite_estimate_alert_is_labelled(self):
        apps.get_model("airquality", "AQIGridPoint").objects.create(
            lat=28.66, lon=77.33, timestamp=self.now - timedelta(hours=1), aqi=260, model_version="x")
        p = mk_profile(lat=28.70, lon=77.40)
        self.run_for(p)
        log = self.logs(p)[0]
        self.assertEqual(log.data_source, "satellite_model")
        self.assertIn("estimated from satellite data", log.message)

    def test_hindi_user_gets_hindi(self):
        self.reading(250)
        p = mk_profile(language="hi")
        self.run_for(p)
        self.assertIn("खराब", self.logs(p)[0].message)

    # --- limits
    def test_cooldown_blocks_a_repeat(self):
        self.reading(250)
        p = mk_profile()
        self.run_for(p)
        s = self.run_for(p)
        self.assertEqual(s["suppressed"], {"cooldown": 1})
        self.assertEqual(len(self.logs(p)), 1)

    def test_a_worse_category_breaks_through_the_cooldown(self):
        self.reading(250, hours_ago=1)
        p = mk_profile()
        self.run_for(p)
        self.reading(350, hours_ago=0.5)
        self.assertEqual(self.run_for(p)["alerts"], 1)
        self.assertEqual([l.severity for l in self.logs(p)], [3, 4])

    def test_same_alert_returns_after_the_cooldown(self):
        self.reading(250)
        p = mk_profile()
        self.run_for(p)
        later = self.now + timedelta(hours=7)
        self.reading(250, hours_ago=0.5, at=later)
        self.assertEqual(self.run_for(p, now=later)["alerts"], 1)

    def test_quiet_hours_hold_back_everything_below_very_poor(self):
        night = datetime(2026, 11, 20, 18, 0, tzinfo=dt_tz.utc)   # 23:30 in India
        p = mk_profile(quiet_start=time(22, 0), quiet_end=time(7, 0))
        self.reading(250, at=night)
        self.assertEqual(self.run_for(p, now=night)["suppressed"], {"quiet_hours": 1})
        self.reading(350, hours_ago=0.5, at=night)
        self.assertEqual(self.run_for(p, now=night)["alerts"], 1)      # severe alerts still pass

    def test_daily_cap(self):
        self.reading(250, hours_ago=1)
        p = mk_profile(max_alerts_per_day=1)
        self.run_for(p)
        self.reading(350, hours_ago=0.5)
        self.assertEqual(self.run_for(p)["suppressed"], {"daily_cap": 1})

    def test_consent_and_channels_are_required(self):
        self.reading(250)
        self.assertEqual(self.run_for(mk_profile(consent_at=None))["suppressed"], {"no_consent": 1})
        self.assertEqual(self.run_for(mk_profile(name="C", push_enabled=False))["suppressed"], {"no_channel": 1})
        self.assertEqual(AlertLog.objects.count(), 0)

    def test_the_default_run_skips_users_without_consent_or_inactive(self):
        self.reading(250)
        mk_profile(consent_at=None)
        mk_profile(name="D", is_active=False)
        self.assertEqual(runner.evaluate_profiles(now=self.now, dry_run=True)["profiles"], 0)

    # --- other alert types
    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_forecast_alert(self):
        self.reading(90)
        self.forecast(160)
        p = mk_profile(persona="asthma")
        self.run_for(p)
        log = self.logs(p)[0]
        self.assertEqual((log.alert_type, log.aqi_value), ("forecast_aqi", 160.0))
        self.assertIn("24 hours", log.message)

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_forecast_not_sent_when_current_alert_already_covers_it(self):
        self.reading(150)
        self.forecast(260)
        p = mk_profile(persona="asthma")
        self.run_for(p)
        self.assertEqual({l.alert_type for l in self.logs(p)}, {"current_aqi"})

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_demo_persistence_forecasts_never_alert_unless_allowed(self):
        self.reading(90)
        self.forecast(160, version="persistence")
        p = mk_profile(persona="asthma")
        self.assertEqual(self.run_for(p)["alerts"], 0)
        with override_settings(ALERTS_ALLOW_BASELINE_FORECAST=True):
            self.assertEqual(self.run_for(mk_profile(persona="asthma", name="E"))["alerts"], 1)

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_smoke_alert_needs_no_aqi_reading(self):
        self.smoke(hours=6)
        p = mk_profile()
        self.run_for(p)
        log = self.logs(p)[0]
        self.assertEqual((log.alert_type, log.severity), ("smoke_arrival", 3))
        self.assertIn("6 hours", log.message)

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_smoke_too_far_in_the_future_is_ignored(self):
        self.smoke(hours=48)
        self.assertEqual(self.run_for(mk_profile())["alerts"], 0)

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_stagnation_alert(self):
        self.reading(120)
        self.weather(wind=2.0, blh=1000.0)
        p = mk_profile(persona="asthma")
        p.rules.filter(alert_type="current_aqi").update(is_active=False)
        self.run_for(p)
        self.assertEqual({l.alert_type for l in self.logs(p)}, {"stagnation"})

    @skipUnless(HAS_WEATHER, "weather app not installed")
    def test_only_one_alert_per_run_and_the_most_severe_wins(self):
        self.reading(120)
        self.weather()
        self.smoke(hours=6)       # smoke severity 3 beats current AQI 120 (severity 2)
        p = mk_profile(persona="asthma")
        s = self.run_for(p)
        self.assertEqual(s["alerts"], 1)
        self.assertEqual(self.logs(p)[0].alert_type, "smoke_arrival")
        self.assertGreaterEqual(s["suppressed"].get("per_run_limit", 0), 1)


    def test_sms_and_voice_wait_for_a_verified_phone(self):
        self.reading(250)
        p = mk_profile(sms_enabled=True, phone_verified_at=None)
        self.run_for(p)
        self.assertEqual([l.channel for l in AlertLog.objects.filter(profile=p)], ["push"])   # no SMS yet
        only_sms = mk_profile(name="G", push_enabled=False, sms_enabled=True, phone_verified_at=None)
        self.assertEqual(self.run_for(only_sms)["suppressed"], {"no_channel": 1})
        with override_settings(ALERTS_REQUIRE_VERIFIED_PHONE=False):
            self.assertEqual(self.run_for(only_sms)["alerts"], 1)

    # --- delivery
    def test_live_send_records_the_provider_id(self):
        self.reading(250)
        p = mk_profile()
        fake = Fake()
        with mock.patch.dict(channels.SENDERS, {"push": fake}):
            s = self.run_for(p, dry_run=False)
        self.assertEqual(s["logs"], {"sent": 1})
        self.assertEqual((self.logs(p)[0].status, self.logs(p)[0].provider_id), ("sent", "fake-1"))
        self.assertEqual(len(fake.calls), 1)

    def test_one_failing_channel_does_not_stop_the_others(self):
        self.reading(250)
        p = mk_profile(sms_enabled=True)
        with mock.patch.dict(channels.SENDERS, {"push": Fake(fail="boom"), "sms": Fake()}), \
                self.assertLogs(runner.logger, level="WARNING"):
            s = self.run_for(p, dry_run=False)
        self.assertEqual(s["logs"], {"failed": 1, "sent": 1})
        failed = AlertLog.objects.get(profile=p, channel="push")
        self.assertIn("boom", failed.error)

    def test_failed_sends_are_not_retried_in_a_tight_loop(self):
        self.reading(250)
        p = mk_profile()
        with mock.patch.dict(channels.SENDERS, {"push": Fake(fail="down")}), \
                self.assertLogs(runner.logger, level="WARNING"):
            self.run_for(p, dry_run=False)
            self.assertEqual(self.run_for(p, dry_run=False)["suppressed"], {"retry_backoff": 1})

    def test_unconfigured_provider_fails_clearly(self):
        self.reading(250)
        p = mk_profile(sms_enabled=True, push_enabled=False)
        with self.assertLogs(runner.logger, level="WARNING"):
            self.run_for(p, dry_run=False)
        self.assertEqual(AlertLog.objects.get(profile=p).status, "failed")
        self.assertIn("twilio", AlertLog.objects.get(profile=p).error.lower())

    def test_voice_only_for_severe_alerts_to_opted_in_users(self):
        p = mk_profile(persona="elderly", voice_enabled=True, push_enabled=False)
        self.reading(250)
        self.assertEqual(self.run_for(p)["suppressed"], {"no_channel": 1})   # voice needs AQI >= 301
        self.reading(350, hours_ago=0.5)
        self.run_for(p)
        self.assertEqual([l.channel for l in AlertLog.objects.filter(profile=p)], ["voice"])

    def test_error_in_one_profile_does_not_stop_the_run(self):
        self.reading(250)
        mk_profile()
        mk_profile(name="F")
        with mock.patch.object(runner, "process_profile", side_effect=[RuntimeError("x"), None]), \
                self.assertLogs(runner.logger, level="ERROR"):
            s = runner.evaluate_profiles(now=self.now, dry_run=True)
        self.assertEqual((s["profiles"], s["suppressed"]), (2, {"error": 1}))


class SenderTests(SimpleTestCase):
    def test_voice_text_is_escaped_in_twiml(self):
        captured = {}

        class FakeCalls:
            def create(self, **kw):
                captured.update(kw)
                return mock.Mock(sid="CA1")

        client = mock.Mock(calls=FakeCalls())
        with mock.patch.object(channels, "_twilio", return_value=(client, "+1000")):
            sid = channels.VoiceSender().send(UserProfile(phone="+919876543210", language="hi"),
                                              mock.Mock(body="AQI < 300 & rising"))
        self.assertEqual(sid, "CA1")
        self.assertIn("AQI &lt; 300 &amp; rising", captured["twiml"])
        self.assertIn('language="hi-IN"', captured["twiml"])

    def test_missing_credentials_raise_not_configured(self):
        with override_settings(ALERTS_TWILIO_SID=None), mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(channels.SendError):
                channels.SmsSender().send(UserProfile(phone="+919876543210"), mock.Mock(body="x"))


@override_settings(ROOT_URLCONF=__name__)
class APITests(APITestCase, BaseData):
    def setUp(self):
        BaseData.setUp(self)
        self.client.default_format = "json"        # whatever the project sets as the test default
        throttle = mock.patch.object(APIView, "throttle_classes", [])   # project-wide throttles must not
        throttle.start()                                                # make the tests flaky
        self.addCleanup(throttle.stop)

    def twilio_post(self, params, signature=""):
        """Post exactly what Twilio sends: a urlencoded form."""
        return self.client.generic("POST", "/api/alerts/webhooks/twilio-sms/", urlencode(params),
                                   content_type="application/x-www-form-urlencoded",
                                   HTTP_X_TWILIO_SIGNATURE=signature)

    PAYLOAD = dict(name="Asha", fcm_token="tok", persona="asthma", language="hi", lat=28.65, lon=77.32, consent=True)

    def register(self, **extra):
        r = self.client.post("/api/alerts/profiles/", dict(self.PAYLOAD, **extra), format="json")
        return r, {"HTTP_X_ALERT_TOKEN": r.json().get("access_token", "")}

    def test_register_creates_profile_rules_and_a_secret_token(self):
        r, auth = self.register()
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertTrue(body["access_token"])
        profile = UserProfile.objects.get()
        self.assertNotEqual(profile.access_token_hash, body["access_token"])      # only a hash is stored
        self.assertEqual(profile.rules.count(), 4)
        self.assertEqual(profile.rules.get(alert_type="current_aqi").aqi_threshold, 101)
        self.assertIsNotNone(profile.consent_at)

    def test_registration_validation(self):
        bad = [dict(consent=False), dict(phone="+91123", sms_enabled=True), dict(sms_enabled=True),
               dict(fcm_token=""), dict(lat=95), dict(persona="alien"), dict(timezone="Mars/Base"),
               dict(quiet_start="22:00:00")]
        for extra in bad:
            self.assertEqual(self.client.post("/api/alerts/profiles/", dict(self.PAYLOAD, **extra),
                                              format="json").status_code, 400, extra)
        self.assertEqual(UserProfile.objects.count(), 0)

    def test_sms_only_registration(self):
        r, _ = self.register(push_enabled=False, fcm_token="", phone="+919876543210", sms_enabled=True)
        self.assertEqual(r.status_code, 201)

    def test_token_is_required_and_scopes_access(self):
        _, auth = self.register()
        self.assertEqual(self.client.get("/api/alerts/profiles/me/", **auth).json()["name"], "Asha")
        self.assertEqual(self.client.get("/api/alerts/profiles/me/").status_code, 401)
        self.assertEqual(self.client.get("/api/alerts/profiles/me/", HTTP_X_ALERT_TOKEN="nope").status_code, 401)
        self.assertNotIn("access_token", self.client.get("/api/alerts/profiles/me/", **auth).json())

    def test_update_profile_and_reset_rules_for_new_persona(self):
        _, auth = self.register()
        r = self.client.patch("/api/alerts/profiles/me/", dict(quiet_start="22:00", quiet_end="07:00",
                              persona="elderly", reset_rules=True), format="json", **auth)
        self.assertEqual(r.status_code, 200)
        profile = UserProfile.objects.get()
        self.assertEqual(profile.quiet_start, time(22, 0))
        self.assertTrue(profile.rules.get(alert_type="current_aqi").allow_voice)

    def test_rules_can_be_edited_only_by_their_owner(self):
        _, auth = self.register()
        rules = self.client.get("/api/alerts/rules/", **auth).json()
        self.assertEqual(len(rules), 4)
        rid = rules[0]["id"]
        self.assertEqual(self.client.patch(f"/api/alerts/rules/{rid}/", dict(aqi_threshold=150, cooldown_hours=3),
                                           format="json", **auth).json()["aqi_threshold"], 150)
        self.assertEqual(self.client.patch(f"/api/alerts/rules/{rid}/", dict(aqi_threshold=0), format="json",
                                           **auth).status_code, 400)
        _, other = self.register(name="Other")
        self.assertEqual(self.client.patch(f"/api/alerts/rules/{rid}/", dict(aqi_threshold=300), format="json",
                                           **other).status_code, 404)

    def test_alert_history(self):
        _, auth = self.register()
        self.reading(150)
        self.run_for(UserProfile.objects.get())
        logs = self.client.get("/api/alerts/logs/", **auth).json()
        self.assertEqual((len(logs), logs[0]["alert_type"], logs[0]["status"]), (1, "current_aqi", "dry_run"))
        self.assertEqual(self.client.get("/api/alerts/logs/").status_code, 401)

    def test_preview_shows_the_message_and_stores_nothing(self):
        self.reading(250)
        r = self.client.post("/api/alerts/preview/", dict(lat=28.65, lon=77.32, persona="general", language="en"),
                             format="json")
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["aqi"]["value"], 250.0)
        self.assertEqual([a["type"] for a in body["alerts"]], ["current_aqi"])
        self.assertIn("Poor", body["alerts"][0]["message"])
        self.assertEqual((UserProfile.objects.count(), AlertLog.objects.count()), (0, 0))
        hi = self.client.post("/api/alerts/preview/", dict(lat=28.65, lon=77.32, language="hi"), format="json").json()
        self.assertIn("खराब", hi["alerts"][0]["message"])
        self.assertEqual(self.client.post("/api/alerts/preview/", dict(lat=95, lon=1), format="json").status_code, 400)

    def test_unsubscribe_stops_alerts(self):
        _, auth = self.register()
        self.assertEqual(self.client.post("/api/alerts/profiles/me/unsubscribe/", **auth).status_code, 200)
        profile = UserProfile.objects.get()
        self.assertFalse(profile.is_active or profile.push_enabled)
        self.reading(400)
        self.assertEqual(runner.evaluate_profiles(now=self.now, dry_run=True)["profiles"], 0)

    def test_delete_my_data(self):
        _, auth = self.register()
        self.assertEqual(self.client.delete("/api/alerts/profiles/me/", **auth).status_code, 204)
        self.assertEqual((UserProfile.objects.count(), AlertRule.objects.count()), (0, 0))

    def test_staff_endpoints(self):
        self.assertIn(self.client.post("/api/alerts/evaluate/", {}, format="json").status_code, (401, 403))
        self.assertIn(self.client.get("/api/alerts/stats/").status_code, (401, 403))
        self.client.force_authenticate(get_user_model().objects.create_user("s", password="x", is_staff=True))
        self.register()
        self.reading(150)
        r = self.client.post("/api/alerts/evaluate/", {}, format="json")
        self.assertEqual((r.status_code, r.json()["dry_run"], r.json()["alerts"]), (200, True, 1))
        stats = self.client.get("/api/alerts/stats/").json()
        self.assertEqual(stats["last_24h"]["by_status"], {"dry_run": 1})


    # --- phone verification
    def sms_user(self):
        r, auth = self.register(push_enabled=False, fcm_token="", phone="+919876543210", sms_enabled=True)
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["phone_verified"])
        return auth

    def test_phone_verification_flow(self):
        auth = self.sms_user()
        r = self.client.post("/api/alerts/profiles/me/phone/verify/", **auth)
        self.assertEqual(r.status_code, 200)
        code = r.json()["debug_code"]                       # only shown in dry-run mode
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/confirm/", dict(code="000000" if code != "000000" else "111111"),
                                          format="json", **auth).status_code, 400)
        ok = self.client.post("/api/alerts/profiles/me/phone/confirm/", dict(code=code), format="json", **auth)
        self.assertEqual((ok.status_code, ok.json()), (200, {"verified": True}))
        self.assertTrue(self.client.get("/api/alerts/profiles/me/", **auth).json()["phone_verified"])
        self.assertEqual(UserProfile.objects.get().otp_hash, "")

    def test_codes_are_throttled_limited_and_expire(self):
        auth = self.sms_user()
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/verify/", **auth).status_code, 200)
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/verify/", **auth).status_code, 429)
        p = UserProfile.objects.get()
        for _ in range(verification.MAX_ATTEMPTS):
            self.assertFalse(verification.confirm(p, "not-it")[0])
        self.assertIn("Too many", verification.confirm(p, "anything")[1])       # locked, even for the right code
        p.otp_attempts = 0
        p.otp_expires_at = timezone.now() - timedelta(minutes=1)
        self.assertIn("expired", verification.confirm(p, "x")[1])

    def test_verification_endpoints_need_a_token_and_a_phone(self):
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/verify/").status_code, 401)
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/confirm/", dict(code="1")).status_code, 401)
        _, auth = self.register()           # push-only user with no phone
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/verify/", **auth).status_code, 400)
        self.assertEqual(self.client.post("/api/alerts/profiles/me/phone/confirm/", dict(code="1"),
                                          format="json", **auth).status_code, 400)

    @override_settings(ALERTS_DRY_RUN=False)
    def test_live_mode_texts_the_code_and_never_returns_it(self):
        auth = self.sms_user()
        fake = Fake()
        with mock.patch.dict(channels.SENDERS, {"sms": fake}):
            r = self.client.post("/api/alerts/profiles/me/phone/verify/", **auth)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("debug_code", r.json())
        self.assertRegex(fake.calls[0], r"verification code is \d{6}")

    @override_settings(ALERTS_DRY_RUN=False)
    def test_a_failed_text_reports_an_error(self):
        auth = self.sms_user()
        with mock.patch.dict(channels.SENDERS, {"sms": Fake(fail="no credit")}):
            r = self.client.post("/api/alerts/profiles/me/phone/verify/", **auth)
        self.assertEqual(r.status_code, 502)

    def test_changing_the_phone_removes_verification(self):
        auth = self.sms_user()
        code = self.client.post("/api/alerts/profiles/me/phone/verify/", **auth).json()["debug_code"]
        self.client.post("/api/alerts/profiles/me/phone/confirm/", dict(code=code), format="json", **auth)
        self.client.patch("/api/alerts/profiles/me/", dict(name="New name"), format="json", **auth)
        self.assertTrue(UserProfile.objects.get().phone_verified_at)            # unrelated edits keep it
        self.client.patch("/api/alerts/profiles/me/", dict(phone="+919811122233"), format="json", **auth)
        self.assertIsNone(UserProfile.objects.get().phone_verified_at)

    # --- Twilio STOP webhook
    @override_settings(ALERTS_TWILIO_TOKEN="secret", ALERTS_TWILIO_WEBHOOK_URL="https://example.com/hook")
    def test_sms_stop_webhook_checks_the_signature(self):
        p = mk_profile(phone="+919876543210", sms_enabled=True, voice_enabled=True)
        params = {"From": "+919876543210", "Body": "STOP"}
        sig = twilio_signature("https://example.com/hook", params, "secret")
        self.assertEqual(self.twilio_post(params, "forged").status_code, 403)
        p.refresh_from_db()
        self.assertTrue(p.sms_enabled)
        self.assertEqual(self.twilio_post(params, sig).status_code, 200)
        p.refresh_from_db()
        self.assertFalse(p.sms_enabled or p.voice_enabled)

    @override_settings(ALERTS_TWILIO_TOKEN="secret", ALERTS_TWILIO_WEBHOOK_URL="https://example.com/hook")
    def test_other_sms_replies_change_nothing(self):
        p = mk_profile(phone="+919876543210", sms_enabled=True)
        params = {"From": "+919876543210", "Body": "thanks"}
        sig = twilio_signature("https://example.com/hook", params, "secret")
        self.assertEqual(self.twilio_post(params, sig).status_code, 200)
        p.refresh_from_db()
        self.assertTrue(p.sms_enabled)

    @override_settings(ALERTS_TWILIO_TOKEN=None)
    def test_webhook_without_twilio_configured_is_503(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.twilio_post({"Body": "STOP"}).status_code, 503)


class CommandAndTaskTests(BaseData):
    def test_evaluate_command_defaults_to_dry_run(self):
        self.reading(250)
        mk_profile()
        out = StringIO()
        call_command("alerts_jobs", "evaluate", "--dry-run", stdout=out)
        self.assertIn("'alerts': 1", out.getvalue())
        self.assertEqual(AlertLog.objects.get().status, "dry_run")

    def test_seed_templates_is_idempotent_and_editable(self):
        call_command("alerts_jobs", "seed-templates", stdout=StringIO())
        self.assertEqual(MessageTemplate.objects.count(), 24)     # 4 types x 2 languages x 3 channels
        call_command("alerts_jobs", "seed-templates", stdout=StringIO())
        self.assertEqual(MessageTemplate.objects.count(), 24)

    def test_purge_logs(self):
        self.reading(250)
        p = mk_profile()
        self.run_for(p)
        AlertLog.objects.update(created_at=timezone.now() - timedelta(days=100))
        call_command("alerts_jobs", "purge-logs", "--days", "90", stdout=StringIO())
        self.assertEqual(AlertLog.objects.count(), 0)

    def test_celery_task_names_are_unique(self):
        from . import tasks
        names = {tasks.evaluate.name, tasks.purge_logs.name}
        self.assertTrue(all(n.startswith("alerts.") for n in names))
        self.assertEqual(len(names), 2)
