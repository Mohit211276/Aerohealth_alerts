from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework import serializers

from .models import AlertLog, AlertRule, UserProfile


class ProfileSerializer(serializers.ModelSerializer):
    consent = serializers.BooleanField(write_only=True, required=False)
    phone_verified = serializers.SerializerMethodField()

    def get_phone_verified(self, obj):
        return obj.phone_verified_at is not None

    class Meta:
        model = UserProfile
        fields = ["public_id", "name", "phone", "fcm_token", "language", "persona", "lat", "lon",
                  "push_enabled", "sms_enabled", "voice_enabled", "quiet_start", "quiet_end", "timezone",
                  "max_alerts_per_day", "is_active", "consent_at", "created_at", "consent", "phone_verified"]
        read_only_fields = ["public_id", "is_active", "consent_at", "created_at"]

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except Exception:
            raise serializers.ValidationError("Unknown time zone, e.g. Asia/Kolkata")
        return value

    def validate(self, attrs):
        inst = self.instance

        def get(key, default=None):
            return attrs.get(key, getattr(inst, key, default) if inst is not None else default)

        if inst is None and not attrs.get("consent"):
            raise serializers.ValidationError({"consent": "You must accept alerts (consent=true) to register."})
        if get("push_enabled", True) and not get("fcm_token", ""):
            raise serializers.ValidationError({"fcm_token": "Needed for push alerts; or set push_enabled=false."})
        if (get("sms_enabled", False) or get("voice_enabled", False)) and not get("phone", ""):
            raise serializers.ValidationError({"phone": "Needed for SMS or voice alerts."})
        if inst is None and not (get("push_enabled", True) or get("sms_enabled", False) or get("voice_enabled", False)):
            raise serializers.ValidationError("Enable at least one channel (push, sms or voice).")
        if bool(get("quiet_start")) != bool(get("quiet_end")):
            raise serializers.ValidationError("Set both quiet_start and quiet_end, or neither.")
        return attrs

    def create(self, validated_data):
        validated_data.pop("consent", None)
        return UserProfile.objects.create(consent_at=timezone.now(), **validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("consent", None)
        if "phone" in validated_data and validated_data["phone"] != instance.phone:
            validated_data.update(phone_verified_at=None, otp_hash="", otp_expires_at=None, otp_attempts=0)
        return super().update(instance, validated_data)


class RuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRule
        fields = ["id", "alert_type", "is_active", "aqi_threshold", "horizon_hours", "radius_km",
                  "cooldown_hours", "allow_voice", "voice_min_aqi"]
        read_only_fields = ["id", "alert_type"]

    def validate_voice_min_aqi(self, value):
        if not 1 <= value <= 500:
            raise serializers.ValidationError("Must be between 1 and 500.")
        return value


class LogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertLog
        fields = ["id", "event_id", "alert_type", "channel", "status", "severity", "language", "title",
                  "message", "aqi_value", "data_source", "error", "created_at"]


class PreviewSerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
    persona = serializers.ChoiceField(choices=UserProfile.Persona.choices, default="general")
    language = serializers.ChoiceField(choices=UserProfile.Language.choices, default="en")
