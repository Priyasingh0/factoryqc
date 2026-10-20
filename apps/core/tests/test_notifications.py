from django.db.models import ProtectedError
from django.test import TestCase

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.core.models import Notification


class NotificationModelTests(TestCase):
    def test_defaults_to_pending_email(self):
        note = Notification.objects.create(subject="PM overdue", body="Lathe #04 PM overdue")
        self.assertEqual(note.channel, Notification.Channel.EMAIL)
        self.assertEqual(note.status, Notification.Status.PENDING)
        self.assertIsNone(note.sent_at)
        self.assertEqual(str(note), "[EMAIL] PM overdue")

    def test_recipient_with_notifications_cannot_be_deleted(self):
        user = make_user("qh", Role.QUALITY_HEAD)
        Notification.objects.create(recipient=user, subject="CTQ out of spec", body="x")
        with self.assertRaises(ProtectedError):
            user.delete()
