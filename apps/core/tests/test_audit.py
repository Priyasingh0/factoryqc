from django.contrib import admin
from django.contrib.auth.models import Permission
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.helpers import TEST_PASSWORD, make_user
from apps.core.audit import log_event
from apps.core.exceptions import ImmutableRecordError
from apps.core.models import AuditLog


class AuditLogImmutabilityTests(TestCase):
    def setUp(self):
        self.entry = log_event(action=AuditLog.Action.OTHER, remarks="initial")

    def test_cannot_modify_saved_entry(self):
        self.entry.remarks = "tampered"
        with self.assertRaises(ImmutableRecordError):
            self.entry.save()
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.remarks, "initial")

    def test_cannot_delete_instance(self):
        with self.assertRaises(ImmutableRecordError):
            self.entry.delete()
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_queryset_update_is_blocked(self):
        with self.assertRaises(ImmutableRecordError):
            AuditLog.objects.all().update(remarks="x")

    def test_queryset_delete_is_blocked(self):
        with self.assertRaises(ImmutableRecordError):
            AuditLog.objects.all().delete()
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_bulk_update_is_blocked(self):
        self.entry.remarks = "x"
        with self.assertRaises(ImmutableRecordError):
            AuditLog.objects.bulk_update([self.entry], ["remarks"])

    def test_no_change_or_delete_permissions_exist(self):
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="core", content_type__model="auditlog"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {"add_auditlog", "view_auditlog"})

    def test_admin_is_read_only_even_for_superuser(self):
        boss = User.objects.create_superuser(
            username="boss", email="b@x.com", password=TEST_PASSWORD, employee_id="E-boss"
        )
        request = RequestFactory().get("/")
        request.user = boss
        model_admin = admin.site._registry[AuditLog]
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_user_with_audit_history_cannot_be_deleted(self):
        user = make_user("worker", Role.QA_INSPECTOR)
        log_event(action=AuditLog.Action.OTHER, user=user)
        with self.assertRaises(ProtectedError):
            user.delete()


class AuditLogContentTests(TestCase):
    def setUp(self):
        self.user = make_user("worker", Role.QA_INSPECTOR)

    def test_login_records_ip_and_device_serial(self):
        self.client.post(
            reverse("accounts:login"),
            {"username": "worker", "password": TEST_PASSWORD},
            REMOTE_ADDR="10.1.2.3",
            HTTP_X_DEVICE_SERIAL="TAB-001",
        )
        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN)
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.ip_address, "10.1.2.3")
        self.assertEqual(entry.device_serial, "TAB-001")

    def test_failed_login_is_audited_without_password(self):
        self.client.post(
            reverse("accounts:login"), {"username": "worker", "password": "wrong-pass"}
        )
        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        self.assertIsNone(entry.user)
        self.assertEqual(entry.user_label, "worker")
        self.assertNotIn("wrong-pass", entry.remarks)

    def test_logout_is_audited(self):
        self.client.login(username="worker", password=TEST_PASSWORD)
        self.client.post(reverse("accounts:logout"))
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGOUT).exists())

    def test_log_event_records_model_pk_and_old_new_values(self):
        entry = log_event(
            action=AuditLog.Action.UPDATE,
            user=self.user,
            instance=self.user,
            field_name="role",
            previous_value="QA_INSPECTOR",
            new_value="SHIFT_SUPERVISOR",
        )
        self.assertEqual(entry.model_name, "accounts.User")
        self.assertEqual(entry.object_id, str(self.user.pk))
        self.assertEqual(entry.previous_value, "QA_INSPECTOR")
        self.assertEqual(entry.new_value, "SHIFT_SUPERVISOR")
        self.assertIn("E-worker", entry.user_label)
