from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.helpers import TEST_PASSWORD, make_user
from apps.core.models import AuditLog
from apps.plant.models import MachinePMCheck, Part, Shift

from .helpers import at, make_gauge, make_machine

FIXED_NOW = at(2026, 10, 14, 10)  # Morning shift


class PlantViewPermissionTests(TestCase):
    def setUp(self):
        call_command("setup_roles", stdout=StringIO())
        self.machine = make_machine()
        self.urls = [
            reverse("plant:machine_list"),
            reverse("plant:machine_detail", args=[self.machine.pk]),
            reverse("plant:gauge_list"),
            reverse("plant:part_list"),
        ]

    def login(self, role):
        username = role.value.lower()
        make_user(username, role)
        self.client.login(username=username, password=TEST_PASSWORD)

    def test_anonymous_is_redirected_to_login(self):
        for url in self.urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn(reverse("accounts:login"), response["Location"])

    def test_inspector_can_view_machines_and_gauges_but_not_parts(self):
        self.login(Role.QA_INSPECTOR)
        self.assertEqual(self.client.get(self.urls[0]).status_code, 200)
        self.assertEqual(self.client.get(self.urls[2]).status_code, 200)
        self.assertEqual(self.client.get(self.urls[3]).status_code, 403)

    def test_dock_clerk_has_no_plant_access(self):
        self.login(Role.DOCK_CLERK)
        for url in self.urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_plant_manager_can_view_everything(self):
        self.login(Role.PLANT_MANAGER)
        for url in self.urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class PlantViewContentTests(TestCase):
    def setUp(self):
        call_command("setup_roles", stdout=StringIO())
        self.machine = make_machine()
        make_user("mgr", Role.PLANT_MANAGER)
        self.client.login(username="mgr", password=TEST_PASSWORD)

    def test_machine_list_shows_locked_machine_with_reason(self):
        response = self.client.get(reverse("plant:machine_list"))
        self.assertContains(response, "CNC-04")
        self.assertContains(response, "Locked")
        self.assertContains(response, "not recorded")

    def test_machine_detail_shows_signed_qr_token(self):
        response = self.client.get(reverse("plant:machine_detail", args=[self.machine.pk]))
        self.assertContains(response, self.machine.qr_token)

    def test_gauge_list_flags_expired_gauge(self):
        make_gauge("VC-OLD", date.today() - timedelta(days=2))
        make_gauge("VC-OK")
        response = self.client.get(reverse("plant:gauge_list"))
        self.assertContains(response, "Expired")
        self.assertContains(response, "VC-OLD")
        self.assertContains(response, "Valid")

    def test_part_list_shows_programs(self):
        part = Part.objects.create(part_number="P-100", name="Shaft")
        part.programs.create(program_name="OP20", version="1", is_approved=False)
        response = self.client.get(reverse("plant:part_list"))
        self.assertContains(response, "OP20 v1")
        self.assertContains(response, "not approved")


class PMCheckViewTests(TestCase):
    def setUp(self):
        call_command("setup_roles", stdout=StringIO())
        self.machine = make_machine()
        self.url = reverse("plant:pm_check_create", args=[self.machine.pk])

    def login(self, role, username=None):
        username = username or role.value.lower()
        make_user(username, role)
        self.client.login(username=username, password=TEST_PASSWORD)

    @mock.patch("apps.plant.services.timezone.now", return_value=FIXED_NOW)
    def test_operator_records_check_and_machine_becomes_ready(self, _now):
        self.login(Role.MACHINE_OPERATOR)
        response = self.client.post(
            self.url, {"completed": "on", "safety_zero_point_ok": "on", "remarks": "ok"}
        )
        self.assertRedirects(
            response, reverse("plant:machine_detail", args=[self.machine.pk]),
            fetch_redirect_response=False,
        )
        check = MachinePMCheck.objects.get()
        self.assertEqual((check.date, check.shift), (date(2026, 10, 14), Shift.MORNING))
        self.assertEqual(check.performed_by.username, "machine_operator")
        self.assertTrue(AuditLog.objects.filter(model_name="plant.MachinePMCheck").exists())
        detail = self.client.get(reverse("plant:machine_detail", args=[self.machine.pk]))
        self.assertContains(detail, "Ready.")

    @mock.patch("apps.plant.services.timezone.now", return_value=FIXED_NOW)
    def test_unchecked_boxes_record_a_failed_check(self, _now):
        self.login(Role.MAINTENANCE_SUPERVISOR)
        self.client.post(self.url, {"completed": "on"})
        check = MachinePMCheck.objects.get()
        self.assertTrue(check.completed)
        self.assertFalse(check.safety_zero_point_ok)

    @mock.patch("apps.plant.services.timezone.now", return_value=FIXED_NOW)
    def test_second_post_after_pass_shows_error_and_keeps_record(self, _now):
        self.login(Role.MACHINE_OPERATOR)
        data = {"completed": "on", "safety_zero_point_ok": "on"}
        self.client.post(self.url, data)
        response = self.client.post(self.url, {"completed": "on"}, follow=True)
        self.assertContains(response, "already recorded and passed")
        self.assertTrue(MachinePMCheck.objects.get().is_clear)

    def test_inspector_cannot_record_pm_check(self):
        self.login(Role.QA_INSPECTOR)
        response = self.client.post(self.url, {"completed": "on", "safety_zero_point_ok": "on"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MachinePMCheck.objects.count(), 0)

    def test_get_is_not_allowed(self):
        self.login(Role.MACHINE_OPERATOR)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_record_form_hidden_from_users_without_permission(self):
        self.login(Role.QA_INSPECTOR)
        response = self.client.get(reverse("plant:machine_detail", args=[self.machine.pk]))
        self.assertNotContains(response, "Save PM check")
