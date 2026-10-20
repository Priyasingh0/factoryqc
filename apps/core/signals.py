from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

from .audit import log_event
from .models import AuditLog


@receiver(user_logged_in)
def audit_login(sender, request, user, **kwargs):
    log_event(action=AuditLog.Action.LOGIN, user=user, instance=user)


@receiver(user_logged_out)
def audit_logout(sender, request, user, **kwargs):
    if user is not None:
        log_event(action=AuditLog.Action.LOGOUT, user=user, instance=user)


@receiver(user_login_failed)
def audit_login_failed(sender, credentials, request=None, **kwargs):
    # Only the attempted username is stored. Passwords are never logged.
    attempted = str(credentials.get("username", ""))[:150]
    log_event(
        action=AuditLog.Action.LOGIN_FAILED,
        user_label=attempted,
        remarks=f"Failed login attempt for username '{attempted}'",
    )
