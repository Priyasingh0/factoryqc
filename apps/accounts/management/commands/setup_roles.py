from django.core.management.base import BaseCommand

from apps.accounts.models import Role
from apps.accounts.services import sync_role_permissions


class Command(BaseCommand):
    help = "Create/refresh one Django Group per role and assign its permissions."

    def handle(self, *args, **options):
        missing = sync_role_permissions()
        self.stdout.write(self.style.SUCCESS(f"Synced {len(Role)} role groups."))
        for perm in missing:
            self.stdout.write(self.style.WARNING(f"Permission not found yet: {perm}"))
