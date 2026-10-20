from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.core.models import AuditLog

from .helpers import TEST_PASSWORD, make_user


class UserAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            username="boss", email="b@x.com", password=TEST_PASSWORD, employee_id="E-boss"
        )
        self.client.force_login(self.boss)
        self.target = make_user("worker", Role.QA_INSPECTOR)

    def _change_form_data(self, **overrides):
        joined = timezone.localtime(self.target.date_joined)
        data = {
            "username": self.target.username,
            "first_name": "",
            "last_name": "",
            "email": "",
            "employee_id": self.target.employee_id,
            "role": self.target.role,
            "is_active": "on",
            "groups": [g.pk for g in self.target.groups.all()],
            "date_joined_0": joined.strftime("%Y-%m-%d"),
            "date_joined_1": joined.strftime("%H:%M:%S"),
        }
        data.update(overrides)
        return data

    def test_role_change_in_admin_updates_group_and_writes_audit_row(self):
        url = reverse("admin:accounts_user_change", args=[self.target.pk])
        # The browser re-posts the OLD group, which must not win over the new role.
        response = self.client.post(
            url, self._change_form_data(role=Role.SHIFT_SUPERVISOR)
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))

        self.target.refresh_from_db()
        self.assertEqual(self.target.role, Role.SHIFT_SUPERVISOR)
        self.assertEqual(
            list(self.target.groups.values_list("name", flat=True)),
            [Role.SHIFT_SUPERVISOR.label],
        )

        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, field_name="role")
        self.assertEqual(entry.previous_value, Role.QA_INSPECTOR)
        self.assertEqual(entry.new_value, Role.SHIFT_SUPERVISOR)
        self.assertEqual(entry.user, self.boss)
        self.assertEqual(entry.object_id, str(self.target.pk))

    def test_creating_user_in_admin_is_audited(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "newbie",
                "password1": TEST_PASSWORD,
                "password2": TEST_PASSWORD,
                "employee_id": "E-777",
                "role": Role.DOCK_CLERK,
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        created = User.objects.get(username="newbie")
        self.assertEqual(created.role, Role.DOCK_CLERK)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.CREATE,
                model_name="accounts.User",
                object_id=str(created.pk),
            ).exists()
        )
