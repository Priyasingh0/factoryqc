from datetime import date

from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.helpers import TEST_PASSWORD, make_user
from apps.core.models import AuditLog
from apps.plant.models import Department, Gauge, Machine, MachinePMCheck, Part, Shift

from .helpers import make_gauge, make_machine


class PlantAdminAuditTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            username="boss", email="b@x.com", password=TEST_PASSWORD, employee_id="E-boss"
        )
        self.client.force_login(self.boss)

    def test_creating_machine_is_audited(self):
        dept = Department.objects.create(code="HT", name="Heat Treatment Bay")
        response = self.client.post(
            reverse("admin:plant_machine_add"),
            {"code": "OVEN-01", "name": "Heat Treat Oven", "department": dept.pk, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        machine = Machine.objects.get(code="OVEN-01")
        entry = AuditLog.objects.get(action=AuditLog.Action.CREATE, model_name="plant.Machine")
        self.assertEqual(entry.object_id, str(machine.pk))
        self.assertEqual(entry.user, self.boss)

    def test_changing_gauge_expiry_logs_previous_and_new_value(self):
        gauge = make_gauge("VC-9", date(2026, 12, 31))
        response = self.client.post(
            reverse("admin:plant_gauge_change", args=[gauge.pk]),
            {
                "gauge_id": "VC-9", "gauge_type": gauge.gauge_type, "description": "",
                "calibrated_on": gauge.calibrated_on.isoformat(),
                "calibration_expiry": "2027-03-31", "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, field_name="calibration_expiry")
        self.assertEqual(entry.previous_value, "2026-12-31")
        self.assertEqual(entry.new_value, "2027-03-31")
        self.assertEqual(entry.model_name, "plant.Gauge")

    def test_changing_machine_department_logs_primary_keys(self):
        machine = make_machine()
        other = Department.objects.create(code="FA", name="Final Assembly Line")
        old_dept = machine.department
        self.client.post(
            reverse("admin:plant_machine_change", args=[machine.pk]),
            {"code": machine.code, "name": machine.name, "department": other.pk, "is_active": "on"},
        )
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, field_name="department")
        self.assertEqual(entry.previous_value, str(old_dept.pk))
        self.assertEqual(entry.new_value, str(other.pk))

    def test_deleting_gauge_is_audited(self):
        gauge = make_gauge("VC-DEL")
        response = self.client.post(
            reverse("admin:plant_gauge_delete", args=[gauge.pk]), {"post": "yes"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Gauge.objects.filter(pk=gauge.pk).exists())
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.DELETE, model_name="plant.Gauge").exists()
        )

    def test_tooling_program_inline_changes_are_audited(self):
        part = Part.objects.create(part_number="P-200", name="Gear")
        base = {
            "part_number": "P-200", "name": "Gear",
            "programs-TOTAL_FORMS": "1", "programs-INITIAL_FORMS": "0",
            "programs-MIN_NUM_FORMS": "0", "programs-MAX_NUM_FORMS": "1000",
            "programs-0-program_name": "OP10", "programs-0-version": "A",
            "programs-0-fixture_setup": "", "programs-0-tool_offsets": "",
            "programs-0-part": part.pk,
        }
        url = reverse("admin:plant_part_change", args=[part.pk])
        self.assertEqual(self.client.post(url, base).status_code, 302)
        program = part.programs.get()
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.CREATE, model_name="plant.ToolingProgram",
                object_id=str(program.pk),
            ).exists()
        )

        # Approve the program through the inline: logged as an UPDATE.
        edit = {
            **base,
            "programs-INITIAL_FORMS": "1",
            "programs-0-id": program.pk,
            "programs-0-is_approved": "on",
        }
        self.assertEqual(self.client.post(url, edit).status_code, 302)
        entry = AuditLog.objects.get(
            action=AuditLog.Action.UPDATE, model_name="plant.ToolingProgram", field_name="is_approved"
        )
        self.assertEqual(entry.previous_value, "False")
        self.assertEqual(entry.new_value, "True")

        # Delete it through the inline: logged as a DELETE.
        delete = {**edit, "programs-0-DELETE": "on"}
        delete.pop("programs-0-is_approved")
        self.assertEqual(self.client.post(url, delete).status_code, 302)
        self.assertFalse(part.programs.exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.DELETE, model_name="plant.ToolingProgram"
            ).exists()
        )

    def test_pm_check_admin_is_view_only_even_for_superuser(self):
        request = RequestFactory().get("/")
        request.user = self.boss
        model_admin = admin.site._registry[MachinePMCheck]
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_pm_check_has_no_change_or_delete_permission_rows(self):
        from django.contrib.auth.models import Permission

        codenames = set(
            Permission.objects.filter(
                content_type__app_label="plant", content_type__model="machinepmcheck"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {"add_machinepmcheck", "view_machinepmcheck"})
