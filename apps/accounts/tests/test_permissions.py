from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Role

from .helpers import TEST_PASSWORD, make_user


class RolePermissionTests(TestCase):
    def setUp(self):
        call_command("setup_roles", stdout=StringIO())
        self.users_url = reverse("accounts:user_list")
        self.audit_url = reverse("core:audit_log")

    def login(self, role):
        make_user(role.value.lower(), role)
        self.client.login(username=role.value.lower(), password=TEST_PASSWORD)

    def test_anonymous_is_redirected_to_login(self):
        for url in (self.users_url, self.audit_url):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("accounts:login"), response["Location"])

    def test_inspector_gets_403_on_users_and_audit(self):
        self.login(Role.QA_INSPECTOR)
        self.assertEqual(self.client.get(self.users_url).status_code, 403)
        self.assertEqual(self.client.get(self.audit_url).status_code, 403)

    def test_plant_manager_can_open_users_and_audit(self):
        self.login(Role.PLANT_MANAGER)
        self.assertEqual(self.client.get(self.users_url).status_code, 200)
        self.assertEqual(self.client.get(self.audit_url).status_code, 200)

    def test_quality_manager_sees_audit_but_not_users(self):
        self.login(Role.QUALITY_MANAGER)
        self.assertEqual(self.client.get(self.audit_url).status_code, 200)
        self.assertEqual(self.client.get(self.users_url).status_code, 403)

    def test_inspector_model_permissions_are_empty(self):
        user = make_user("insp", Role.QA_INSPECTOR)
        self.assertFalse(user.has_perm("core.view_auditlog"))
        self.assertFalse(user.has_perm("accounts.view_user"))

    def test_setup_roles_is_idempotent(self):
        group = Group.objects.get(name=Role.PLANT_MANAGER.label)
        before = set(group.permissions.values_list("codename", flat=True))
        call_command("setup_roles", stdout=StringIO())
        after = set(group.permissions.values_list("codename", flat=True))
        self.assertEqual(before, after)
        self.assertIn("view_user", after)
        self.assertEqual(
            Group.objects.filter(name__in=[r.label for r in Role]).count(), len(Role)
        )
