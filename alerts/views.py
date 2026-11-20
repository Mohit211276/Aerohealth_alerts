import base64
import hashlib
import hmac
import os
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .conditions import Conditions
from .engine import evaluate_rules
from .models import AlertLog, AlertRule, UserProfile
from .personas import build_default_rules, category_name, create_default_rules
from .runner import evaluate_profiles, is_dry_run
from .serializers import (LogSerializer, PreviewSerializer, ProfileSerializer, RuleSerializer)
from . import verification
from .templates import render

UNAUTHORIZED = {"error": "A valid X-Alert-Token header is required."}


def profile_from_request(request):
    raw = request.headers.get("X-Alert-Token", "").strip()
    if not raw:
        return None
    return UserProfile.objects.filter(access_token_hash=UserProfile.hash_token(raw)).first()


class TokenView(APIView):
    """Views for the owner of a profile: no session/CSRF, the secret token is the credential."""
    authentication_classes = []
    permission_classes = [AllowAny]


class ProfileCreateView(TokenView):
    def post(self, request):
        serializer = ProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        create_default_rules(profile)
        token = profile.rotate_token()
        data = ProfileSerializer(profile).data
        data["access_token"] = token
        data["note"] = "Save access_token now: it is shown only once. Send it as the X-Alert-Token header."
        return Response(data, status=status.HTTP_201_CREATED)


class MeView(TokenView):
    def get(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        return Response(ProfileSerializer(profile).data)

    def patch(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        old_persona = profile.persona
        serializer = ProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        if profile.persona != old_persona and request.data.get("reset_rules") in (True, "true", "1"):
            profile.rules.all().delete()
            create_default_rules(profile)
        return Response(ProfileSerializer(profile).data)

    def delete(self, request):
        """Delete-my-data: removes the profile, its rules and its alert history."""
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        profile.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UnsubscribeView(TokenView):
    def post(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        profile.is_active = False
        profile.push_enabled = profile.sms_enabled = profile.voice_enabled = False
        profile.save(update_fields=["is_active", "push_enabled", "sms_enabled", "voice_enabled"])
        return Response({"unsubscribed": True})


class PhoneVerifyView(TokenView):
    """Send a one-time code to the profile's phone."""

    def post(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        try:
            code = verification.start(profile)
        except verification.TooSoon as exc:
            return Response({"error": str(exc)}, status=429)
        except verification.NoPhone as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception as exc:
            return Response({"error": f"could not send the code: {exc}"}, status=502)
        data = {"sent": True, "expires_in_minutes": verification.OTP_MINUTES}
        if code is not None:          # only in dry-run mode, when no SMS is really sent
            data["debug_code"] = code
        return Response(data)


class PhoneConfirmView(TokenView):
    def post(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        ok, reason = verification.confirm(profile, str(request.data.get("code", "")).strip())
        if not ok:
            return Response({"error": reason}, status=400)
        return Response({"verified": True})


class MyRulesView(TokenView):
    def get(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        return Response(RuleSerializer(profile.rules.all(), many=True).data)


class MyRuleView(TokenView):
    def _rule(self, request, pk):
        profile = profile_from_request(request)
        if profile is None:
            return None, Response(UNAUTHORIZED, status=401)
        rule = profile.rules.filter(pk=pk).first()
        if rule is None:
            return None, Response({"error": "rule not found"}, status=404)
        return rule, None

    def get(self, request, pk):
        rule, error = self._rule(request, pk)
        return error or Response(RuleSerializer(rule).data)

    def patch(self, request, pk):
        rule, error = self._rule(request, pk)
        if error:
            return error
        serializer = RuleSerializer(rule, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return Response(RuleSerializer(serializer.save()).data)


class MyLogsView(TokenView):
    def get(self, request):
        profile = profile_from_request(request)
        if profile is None:
            return Response(UNAUTHORIZED, status=401)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 50)), 200))
        except ValueError:
            limit = 50
        return Response(LogSerializer(profile.logs.all()[:limit], many=True).data)


class PreviewView(TokenView):
    """POST lat, lon, persona, language -> what a user there would be told right now.
    Nothing is stored and nothing is sent."""

    def post(self, request):
        serializer = PreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        profile = UserProfile(persona=d["persona"], language=d["language"], lat=d["lat"], lon=d["lon"])
        cond = Conditions(profile)
        alerts = []
        for decision in evaluate_rules(build_default_rules(profile), cond):
            msg = render(profile, decision, "push")
            alerts.append({"type": decision.alert_type, "severity": decision.severity,
                           "category": category_name(decision.severity, d["language"]),
                           "title": msg.title, "message": msg.body})
        return Response({"aqi": cond.aqi(), "dispersion": cond.dispersion(), "alerts": alerts,
                         "note": "Preview only: nothing was saved or sent."})


class EvaluateView(APIView):
    """Staff only. Runs the evaluation now. A request can only make it safer (force a dry run)."""
    permission_classes = [IsAdminUser]

    def post(self, request):
        dry = is_dry_run()
        if not dry and request.data.get("dry_run") in (True, "true", "1"):
            dry = True
        profiles = UserProfile.objects.filter(is_active=True, consent_at__isnull=False)
        if request.data.get("profile"):
            profiles = profiles.filter(public_id=request.data["profile"])
        return Response(evaluate_profiles(profiles=profiles, dry_run=dry))


class StatsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        since = timezone.now() - timedelta(hours=24)
        logs = AlertLog.objects.filter(created_at__gte=since)
        by = lambda field: {r[field]: r["n"] for r in logs.values(field).annotate(n=Count("id"))}
        return Response({"dry_run_mode": is_dry_run(),
                         "profiles_active": UserProfile.objects.filter(is_active=True).count(),
                         "last_24h": {"by_status": by("status"), "by_type": by("alert_type"),
                                      "by_channel": by("channel")}})


STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "रोकें"}


def twilio_signature(url, params, token):
    """Twilio's request signature: base64(HMAC-SHA1(token, url + sorted key+value pairs))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


class TwilioSmsWebhookView(TokenView):
    """Twilio inbound-SMS webhook: STOP switches off SMS and voice for that number."""
    # Twilio posts a form. Declared here so the project-wide DRF parser settings cannot reject it.
    parser_classes = [FormParser, MultiPartParser]

    def post(self, request):
        token = getattr(settings, "ALERTS_TWILIO_TOKEN", None) or os.environ.get("TWILIO_AUTH_TOKEN")
        if not token:
            return Response({"error": "Twilio is not configured"}, status=503)
        url = getattr(settings, "ALERTS_TWILIO_WEBHOOK_URL", None) or request.build_absolute_uri()
        params = {k: request.POST.get(k) for k in request.POST}
        expected = twilio_signature(url, params, token)
        if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
            return Response({"error": "invalid signature"}, status=403)
        if params.get("Body", "").strip().lower() in STOP_WORDS and params.get("From"):
            UserProfile.objects.filter(phone=params["From"]).update(sms_enabled=False, voice_enabled=False)
        return HttpResponse("<Response/>", content_type="text/xml")
