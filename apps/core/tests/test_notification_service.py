from django.core import mail
from django.test import TestCase

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.core.models import Notification
from apps.core.notifications import notify_role


class NotifyRoleTests(TestCase):
    def test_emails_every_active_user_with_the_role(self):
        make_user("q1", Role.QUALITY_HEAD, email="q1@factory.local")
        make_user("q2", Role.QUALITY_HEAD, email="q2@factory.local")
        make_user("gone", Role.QUALITY_HEAD, email="gone@factory.local", is_active=False)
        make_user("other", Role.DOCK_CLERK, email="dock@factory.local")
        notes = notify_role(Role.QUALITY_HEAD, "CTQ failure", "Dimension out of spec")
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(n.status == Notification.Status.SENT for n in notes))
        self.assertTrue(all(n.sent_at for n in notes))
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, ["q1@factory.local", "q2@factory.local"])

    def test_user_without_email_is_marked_failed_not_crashed(self):
        make_user("noemail", Role.QUALITY_HEAD)
        note = notify_role(Role.QUALITY_HEAD, "CTQ failure", "x")[0]
        self.assertEqual(note.status, Notification.Status.FAILED)
        self.assertEqual(len(mail.outbox), 0)

    def test_same_subject_for_same_object_is_sent_once(self):
        user = make_user("q1", Role.QUALITY_HEAD, email="q1@factory.local")
        self.assertEqual(len(notify_role(Role.QUALITY_HEAD, "Alert", "x", related=user)), 1)
        self.assertEqual(notify_role(Role.QUALITY_HEAD, "Alert", "x", related=user), [])
        self.assertEqual(Notification.objects.count(), 1)

    def test_same_subject_is_sent_to_each_different_role(self):
        head = make_user("q1", Role.QUALITY_HEAD, email="q1@factory.local")
        manager = make_user("pm", Role.PLANT_MANAGER, email="pm@factory.local")
        for role in (Role.PLANT_MANAGER, Role.QUALITY_HEAD):
            self.assertEqual(len(notify_role(role, "Lot quarantined", "x", related=head)), 1)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(notify_role(Role.QUALITY_HEAD, "Lot quarantined", "x", related=head), [])

    def test_different_subject_is_sent_again(self):
        user = make_user("q1", Role.QUALITY_HEAD, email="q1@factory.local")
        notify_role(Role.QUALITY_HEAD, "Alert 1", "x", related=user)
        notify_role(Role.QUALITY_HEAD, "Alert 2", "x", related=user)
        self.assertEqual(Notification.objects.count(), 2)

    def test_no_recipients_keeps_a_pending_record(self):
        note = notify_role(Role.QUALITY_HEAD, "Alert", "x")[0]
        self.assertIsNone(note.recipient)
        self.assertEqual(note.status, Notification.Status.PENDING)
