import hashlib
import re
import secrets
import uuid

from django.contrib.gis.db import models
from django.contrib.gis.geos import Point
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator

phone_validator = RegexValidator(r"^\+[1-9]\d{7,14}$", "Use international format, e.g. +919876543210")


class UserProfile(models.Model):
    class Persona(models.TextChoices):
        GENERAL = "general", "General"
        ASTHMA = "asthma", "Asthma / respiratory"
        ELDERLY = "elderly", "Elderly"
        OUTDOOR_WORKER = "outdoor_worker", "Outdoor worker"
        CHILD = "child", "Child / school"

    class Language(models.TextChoices):
        EN = "en", "English"
        HI = "hi", "Hindi"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    access_token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=16, blank=True, validators=[phone_validator])
    fcm_token = models.CharField(max_length=255, blank=True)
    language = models.CharField(max_length=5, choices=Language.choices, default=Language.EN)
    persona = models.CharField(max_length=20, choices=Persona.choices, default=Persona.GENERAL)
    lat = models.FloatField(validators=[MinValueValidator(-90), MaxValueValidator(90)])
    lon = models.FloatField(validators=[MinValueValidator(-180), MaxValueValidator(180)])
    point = models.PointField(geography=True, srid=4326, null=True, blank=True, editable=False)
    push_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=False)
    voice_enabled = models.BooleanField(default=False)
    quiet_start = models.TimeField(null=True, blank=True, help_text="Local time; severe alerts still pass")
    quiet_end = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=40, default="Asia/Kolkata")
    max_alerts_per_day = models.PositiveSmallIntegerField(
        default=4, validators=[MinValueValidator(1), MaxValueValidator(20)])
    consent_at = models.DateTimeField(null=True, blank=True, help_text="When the user accepted alerts")
    phone_verified_at = models.DateTimeField(null=True, blank=True, editable=False,
                                             help_text="SMS and voice are only used after the phone is verified")
    otp_hash = models.CharField(max_length=64, blank=True, editable=False)
    otp_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    otp_sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    otp_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "alerts_profile"
        ordering = ["id"]

    def save(self, *args, **kwargs):
        self.point = Point(self.lon, self.lat, srid=4326)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and ("lat" in update_fields or "lon" in update_fields):
            kwargs["update_fields"] = list(set(update_fields) | {"point"})
        super().save(*args, **kwargs)

    @staticmethod
    def hash_token(raw):
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def rotate_token(self):
        """Create a new secret access token; only its hash is stored. Returns the raw token once."""
        raw = secrets.token_urlsafe(32)
        self.access_token_hash = self.hash_token(raw)
        self.save(update_fields=["access_token_hash"])
        return raw

    def __str__(self):
        return f"{self.name or 'Profile'} ({self.persona}, {self.language})"


class AlertRule(models.Model):
    class AlertType(models.TextChoices):
        CURRENT_AQI = "current_aqi", "Current AQI"
        FORECAST_AQI = "forecast_aqi", "Forecast AQI"
        SMOKE_ARRIVAL = "smoke_arrival", "Stubble smoke arrival"
        STAGNATION = "stagnation", "Trapped pollution"

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="rules")
    alert_type = models.CharField(max_length=20, choices=AlertType.choices)
    is_active = models.BooleanField(default=True)
    aqi_threshold = models.PositiveSmallIntegerField(
        default=201, validators=[MinValueValidator(1), MaxValueValidator(500)])
    horizon_hours = models.PositiveSmallIntegerField(
        default=24, validators=[MinValueValidator(1), MaxValueValidator(72)])
    radius_km = models.PositiveSmallIntegerField(
        default=50, validators=[MinValueValidator(1), MaxValueValidator(300)])
    cooldown_hours = models.PositiveSmallIntegerField(
        default=6, validators=[MinValueValidator(1), MaxValueValidator(72)])
    allow_voice = models.BooleanField(default=False)
    voice_min_aqi = models.PositiveSmallIntegerField(default=301)

    class Meta:
        db_table = "alerts_rule"
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["profile", "alert_type"], name="uniq_alert_rule_type")]

    def __str__(self):
        return f"{self.alert_type} >= {self.aqi_threshold}"


class AlertLog(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        DRY_RUN = "dry_run", "Dry run (not sent)"
        FAILED = "failed", "Failed"

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="logs")
    rule = models.ForeignKey(AlertRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs")
    event_id = models.UUIDField(db_index=True, default=uuid.uuid4,
                                help_text="Groups the per-channel rows of one alert")
    alert_type = models.CharField(max_length=20)
    channel = models.CharField(max_length=10)
    status = models.CharField(max_length=10, choices=Status.choices)
    severity = models.PositiveSmallIntegerField(default=0)
    language = models.CharField(max_length=5, default="en")
    title = models.CharField(max_length=200, blank=True)
    message = models.TextField(blank=True)
    aqi_value = models.FloatField(null=True, blank=True)
    data_source = models.CharField(max_length=30, blank=True)
    provider_id = models.CharField(max_length=100, blank=True, help_text="Twilio SID / FCM message id")
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "alerts_log"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["profile", "alert_type", "-created_at"], name="alerts_log_prof_type_idx")]

    def __str__(self):
        return f"{self.alert_type}/{self.channel}/{self.status}"


class MessageTemplate(models.Model):
    """Optional override of the built-in message text. Placeholders: {place} {aqi} {category}
    {hours} {radius} {advice} {source_note}."""
    alert_type = models.CharField(max_length=20, choices=AlertRule.AlertType.choices)
    language = models.CharField(max_length=5, choices=UserProfile.Language.choices)
    channel = models.CharField(max_length=10, choices=[("push", "Push"), ("sms", "SMS"), ("voice", "Voice")])
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "alerts_template"
        constraints = [models.UniqueConstraint(fields=["alert_type", "language", "channel"],
                                               name="uniq_alert_template")]

    def __str__(self):
        return f"{self.alert_type}/{self.language}/{self.channel}"
