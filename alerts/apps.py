from django.apps import AppConfig


class AlertsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    # Works whether installed as "backend.alerts" or "alerts".
    name = __package__
    label = "alerts"
    verbose_name = "Air-quality alerts"
