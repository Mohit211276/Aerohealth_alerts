"""Phone verification by one-time code, so nobody can make us text a number they do not own."""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.utils import timezone

from . import channels
from .runner import is_dry_run
from .templates import Rendered

OTP_MINUTES = 10
RESEND_SECONDS = 60
MAX_ATTEMPTS = 5


class NoPhone(Exception):
    pass


class TooSoon(Exception):
    pass


def _hash(profile, code):
    return hashlib.sha256(f"{profile.public_id}:{code}".encode()).hexdigest()


def start(profile, now=None):
    """Create and send a code. Returns the code only in dry-run mode (nothing is texted then)."""
    now = now or timezone.now()
    if not profile.phone:
        raise NoPhone("Add a phone number first.")
    if profile.otp_sent_at and (now - profile.otp_sent_at).total_seconds() < RESEND_SECONDS:
        raise TooSoon(f"Wait {RESEND_SECONDS} seconds before asking for another code.")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    dry = is_dry_run()
    if not dry:
        channels.send_via("sms", profile, Rendered(
            title="", body=f"Your VayuDrishti verification code is {code}. It expires in {OTP_MINUTES} minutes."))
    profile.otp_hash = _hash(profile, code)
    profile.otp_expires_at = now + timedelta(minutes=OTP_MINUTES)
    profile.otp_sent_at = now
    profile.otp_attempts = 0
    profile.save(update_fields=["otp_hash", "otp_expires_at", "otp_sent_at", "otp_attempts"])
    return code if dry else None


def confirm(profile, code, now=None):
    """Returns (True, "") on success, otherwise (False, reason)."""
    now = now or timezone.now()
    if not profile.otp_hash or not profile.otp_expires_at:
        return False, "No code was requested."
    if now > profile.otp_expires_at:
        return False, "The code has expired; request a new one."
    if profile.otp_attempts >= MAX_ATTEMPTS:
        return False, "Too many wrong attempts; request a new code."
    if not hmac.compare_digest(profile.otp_hash, _hash(profile, code)):
        profile.otp_attempts += 1
        profile.save(update_fields=["otp_attempts"])
        return False, "Wrong code."
    profile.phone_verified_at = now
    profile.otp_hash, profile.otp_expires_at, profile.otp_attempts = "", None, 0
    profile.save(update_fields=["phone_verified_at", "otp_hash", "otp_expires_at", "otp_attempts"])
    return True, ""
