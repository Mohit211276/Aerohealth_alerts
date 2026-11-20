"""
Delivery channels. Nothing is sent unless ALERTS_DRY_RUN is False. Provider libraries are imported
lazily, so the app runs without firebase-admin or twilio installed (sends then fail with a clear error).
"""
import os
from xml.sax.saxutils import escape

from django.conf import settings


class SendError(Exception):
    pass


class NotConfigured(SendError):
    pass


def _cfg(setting, env, default=None):
    return getattr(settings, setting, None) or os.environ.get(env) or default


class PushSender:
    def send(self, profile, msg):
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging
        except ImportError:
            raise NotConfigured("firebase-admin is not installed (pip install firebase-admin)")
        path = _cfg("ALERTS_FCM_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS")
        if not path:
            raise NotConfigured("set ALERTS_FCM_CREDENTIALS to your Firebase service-account JSON path")
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(credentials.Certificate(path))
        message = messaging.Message(notification=messaging.Notification(title=msg.title, body=msg.body),
                                    token=profile.fcm_token)
        return messaging.send(message)


def _twilio():
    try:
        from twilio.rest import Client
    except ImportError:
        raise NotConfigured("twilio is not installed (pip install twilio)")
    sid = _cfg("ALERTS_TWILIO_SID", "TWILIO_ACCOUNT_SID")
    token = _cfg("ALERTS_TWILIO_TOKEN", "TWILIO_AUTH_TOKEN")
    sender = _cfg("ALERTS_TWILIO_FROM", "TWILIO_FROM_NUMBER")
    if not (sid and token and sender):
        raise NotConfigured("set Twilio SID, auth token and from-number (settings or environment)")
    return Client(sid, token), sender


class SmsSender:
    def send(self, profile, msg):
        client, sender = _twilio()
        return client.messages.create(to=profile.phone, from_=sender, body=msg.body).sid


class VoiceSender:
    def send(self, profile, msg):
        client, sender = _twilio()
        languages = getattr(settings, "ALERTS_VOICE_LANGUAGES", {"en": "en-IN", "hi": "hi-IN"})
        lang = languages.get(profile.language, "en-IN")
        twiml = f'<Response><Say language="{lang}">{escape(msg.body)}</Say></Response>'
        return client.calls.create(to=profile.phone, from_=sender, twiml=twiml).sid


SENDERS = {"push": PushSender(), "sms": SmsSender(), "voice": VoiceSender()}


def send_via(channel, profile, msg):
    return SENDERS[channel].send(profile, msg)
