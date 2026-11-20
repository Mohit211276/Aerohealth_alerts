from django.contrib import admin

from .models import AlertLog, AlertRule, MessageTemplate, UserProfile


class RuleInline(admin.TabularInline):
    model = AlertRule
    extra = 0


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "persona", "language", "phone", "push_enabled", "sms_enabled", "voice_enabled",
                    "is_active", "created_at")
    list_filter = ("persona", "language", "is_active")
    search_fields = ("name", "phone")
    readonly_fields = ("public_id", "consent_at", "created_at", "updated_at")
    inlines = [RuleInline]


@admin.register(AlertLog)
class AlertLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "profile", "alert_type", "channel", "status", "severity", "aqi_value")
    list_filter = ("status", "channel", "alert_type")
    date_hierarchy = "created_at"
    raw_id_fields = ("profile", "rule")

    def has_add_permission(self, request):
        return False


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("alert_type", "language", "channel", "is_active")
    list_filter = ("language", "channel", "is_active")
