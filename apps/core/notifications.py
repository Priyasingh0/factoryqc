"""Alert delivery (PRD 2.2 PM alerts, 4.3 CTQ alerts).

Creates a Notification row per recipient and tries to email it. In development
the console email backend prints the message in the terminal.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)


def notify_role(role, subject, body, *, related=None):
    """Alert every active user holding `role`. Returns the Notifications created.

    Duplicate suppression: the same subject about the same related object is
    only sent once to the same role, so a tablet that keeps retrying does not spam people.
    Put the date/shift in the subject if the alert may legitimately repeat.

    NOTE: if the caller raises inside `transaction.atomic`, the notification is
    rolled back with it. Call this before opening the transaction, or catch
    the exception inside it.
    """
    related_model = related._meta.label if related is not None else ""
    related_id = str(related.pk) if related is not None else ""

    if Notification.objects.filter(
        recipient_role=role, subject=subject, related_model=related_model, related_object_id=related_id
    ).exists():
        return []

    base = dict(
        recipient_role=role,
        channel=Notification.Channel.EMAIL,
        subject=subject,
        body=body,
        related_model=related_model,
        related_object_id=related_id,
    )
    recipients = list(get_user_model().objects.filter(role=role, is_active=True))
    if not recipients:
        # Keep the alert on record even though nobody holds the role yet.
        return [Notification.objects.create(status=Notification.Status.PENDING, **base)]

    created = []
    for user in recipients:
        note = Notification.objects.create(recipient=user, **base)
        _deliver(note, user)
        created.append(note)
    return created


def _deliver(note, user):
    if not user.email:
        note.status = Notification.Status.FAILED
    else:
        try:
            send_mail(note.subject, note.body, settings.DEFAULT_FROM_EMAIL, [user.email])
        except Exception:  # delivery problems must never break the business action
            logger.exception("Could not email notification %s", note.pk)
            note.status = Notification.Status.FAILED
        else:
            note.status = Notification.Status.SENT
            note.sent_at = timezone.now()
    note.save()
