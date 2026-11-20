from django.urls import path

from . import views

app_name = "alerts"  # for include("backend.alerts.urls", namespace="alerts")

urlpatterns = [
    path("profiles/", views.ProfileCreateView.as_view(), name="profile-create"),
    path("profiles/me/", views.MeView.as_view(), name="me"),
    path("profiles/me/unsubscribe/", views.UnsubscribeView.as_view(), name="unsubscribe"),
    path("profiles/me/phone/verify/", views.PhoneVerifyView.as_view(), name="phone-verify"),
    path("profiles/me/phone/confirm/", views.PhoneConfirmView.as_view(), name="phone-confirm"),
    path("rules/", views.MyRulesView.as_view(), name="rules"),
    path("rules/<int:pk>/", views.MyRuleView.as_view(), name="rule"),
    path("logs/", views.MyLogsView.as_view(), name="logs"),
    path("preview/", views.PreviewView.as_view(), name="preview"),
    path("evaluate/", views.EvaluateView.as_view(), name="evaluate"),
    path("stats/", views.StatsView.as_view(), name="stats"),
    path("webhooks/twilio-sms/", views.TwilioSmsWebhookView.as_view(), name="twilio-sms"),
]
