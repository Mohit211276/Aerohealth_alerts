from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Alert jobs without Celery: evaluate | seed-templates | purge-logs"

    def add_arguments(self, parser):
        parser.add_argument("job", choices=["evaluate", "seed-templates", "purge-logs"])
        parser.add_argument("--dry-run", action="store_true", help="evaluate: never send, only log")
        parser.add_argument("--profile", default=None, help="evaluate: only this profile public_id")
        parser.add_argument("--days", type=int, default=90, help="purge-logs: keep this many days")

    def handle(self, *args, **opts):
        job = opts["job"]
        if job == "evaluate":
            from ...models import UserProfile
            from ...runner import evaluate_profiles, is_dry_run
            profiles = UserProfile.objects.filter(is_active=True, consent_at__isnull=False)
            if opts["profile"]:
                profiles = profiles.filter(public_id=opts["profile"])
            dry = True if opts["dry_run"] else is_dry_run()
            self.stdout.write("evaluate: %s" % evaluate_profiles(profiles=profiles, dry_run=dry))
        elif job == "seed-templates":
            from ...models import MessageTemplate
            from ...templates import DEFAULTS
            created = 0
            for (alert_type, language), (title, body) in DEFAULTS.items():
                for channel in ("push", "sms", "voice"):
                    _, new = MessageTemplate.objects.get_or_create(
                        alert_type=alert_type, language=language, channel=channel,
                        defaults={"title": title, "body": body})
                    created += new
            self.stdout.write(f"seed-templates: {created} created, edit them in the admin")
        else:
            from ...models import AlertLog
            deleted = AlertLog.objects.filter(created_at__lt=timezone.now() - timedelta(days=opts["days"])).delete()[0]
            self.stdout.write(f"purge-logs: {deleted} deleted")
