from decimal import Decimal

from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.helpers import TEST_PASSWORD, make_user
from apps.checklists.models import ChecklistTemplate, ChecklistType, TemplateStatus
from apps.core.models import AuditLog

from .helpers import RoleTestCase, force_status, make_part, make_template, pdf_upload


class TemplateAdminTests(RoleTestCase):
    """Quality Engineers author drafts in the admin site (needs staff status)."""

    def setUp(self):
        super().setUp()
        for user in (self.engineer, self.manager):
            user.is_staff = True
            user.save()
        self.client.login(username="eng", password=TEST_PASSWORD)
        self.part = make_part()

    def add_data(self, **extra):
        data = {
            "part": self.part.pk, "checklist_type": ChecklistType.PDI, "operation_number": "",
            "customer_agreement": "QA-17", "revision": "A", "drawing_pdf": pdf_upload(),
            "pins-TOTAL_FORMS": "1", "pins-INITIAL_FORMS": "0",
            "pins-MIN_NUM_FORMS": "0", "pins-MAX_NUM_FORMS": "1000",
            "pins-0-number": "1", "pins-0-page": "1", "pins-0-x_pct": "10", "pins-0-y_pct": "20",
            "pins-0-color": "#ff8800",
            "fields-TOTAL_FORMS": "1", "fields-INITIAL_FORMS": "0",
            "fields-MIN_NUM_FORMS": "0", "fields-MAX_NUM_FORMS": "1000",
            "fields-0-sequence": "1", "fields-0-label": "Shaft diameter",
            "fields-0-field_type": "DIMENSION", "fields-0-nominal": "25",
            "fields-0-upper_tol": "0.05", "fields-0-lower_tol": "0.02", "fields-0-unit": "mm",
            "fields-0-is_ctq": "on", "fields-0-required_gauge_type": "MICROMETER",
        }
        data.update(extra)
        return data

    def test_engineer_creates_draft_with_pins_and_fields(self):
        response = self.client.post(reverse("admin:checklists_checklisttemplate_add"), self.add_data())
        self.assertEqual(response.status_code, 302, getattr(response, "context_data", None))
        template = ChecklistTemplate.objects.get()
        self.assertEqual(template.created_by, self.engineer)  # taken from the session, not the form
        self.assertEqual(template.status, TemplateStatus.DRAFT)
        self.assertEqual(template.pins.get().color, "#ff8800")
        field = template.fields.get()
        self.assertTrue(field.is_ctq)
        self.assertEqual(field.nominal, Decimal("25.0000"))
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.CREATE, user=self.engineer,
                                    model_name="checklists.ChecklistTemplate").exists()
        )
        self.assertTrue(AuditLog.objects.filter(model_name="checklists.TemplateField").exists())
        self.assertTrue(AuditLog.objects.filter(model_name="checklists.BlueprintPin").exists())

    def test_non_pdf_drawing_is_rejected_by_the_form(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("drawing.pdf", b"not a pdf at all")
        response = self.client.post(
            reverse("admin:checklists_checklisttemplate_add"), self.add_data(drawing_pdf=bad)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChecklistTemplate.objects.count(), 0)

    def test_invalid_field_numbers_are_rejected_by_the_form(self):
        data = self.add_data(**{"fields-0-lower_tol": "-0.02"})
        response = self.client.post(reverse("admin:checklists_checklisttemplate_add"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChecklistTemplate.objects.count(), 0)

    def test_status_cannot_be_set_from_the_form(self):
        data = self.add_data(status="RELEASED", approved_by="1")
        self.client.post(reverse("admin:checklists_checklisttemplate_add"), data)
        self.assertEqual(ChecklistTemplate.objects.get().status, TemplateStatus.DRAFT)

    def test_released_template_is_read_only_in_admin(self):
        template = make_template(self.engineer)
        force_status(template, TemplateStatus.RELEASED, approver=self.manager)
        url = reverse("admin:checklists_checklisttemplate_change", args=[template.pk])
        # Viewing works; saving is refused.
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {"customer_agreement": "tampered", "revision": "A"})
        self.assertEqual(response.status_code, 403)
        template.refresh_from_db()
        self.assertEqual(template.customer_agreement, "")

    def test_released_template_cannot_be_deleted_in_admin(self):
        template = make_template(self.engineer)
        force_status(template, TemplateStatus.RELEASED, approver=self.manager)
        url = reverse("admin:checklists_checklisttemplate_delete", args=[template.pk])
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(ChecklistTemplate.objects.filter(pk=template.pk).exists())

    def test_draft_can_be_deleted_in_admin_and_is_audited(self):
        template = make_template(self.engineer, with_field=False)
        url = reverse("admin:checklists_checklisttemplate_delete", args=[template.pk])
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 302)
        self.assertFalse(ChecklistTemplate.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DELETE,
                                                model_name="checklists.ChecklistTemplate").exists())

    def test_quality_manager_has_no_authoring_permissions(self):
        self.client.logout()
        self.client.login(username="qm", password=TEST_PASSWORD)
        response = self.client.get(reverse("admin:checklists_checklisttemplate_add"))
        self.assertEqual(response.status_code, 403)

    def test_permission_table(self):
        qe_perms = {p.split(".")[1] for p in self.engineer.get_all_permissions()}
        self.assertIn("add_checklisttemplate", qe_perms)
        self.assertNotIn("approve_checklisttemplate", qe_perms)
        qm_perms = {p.split(".")[1] for p in self.manager.get_all_permissions()}
        self.assertIn("approve_checklisttemplate", qm_perms)
        self.assertNotIn("change_checklisttemplate", qm_perms)
        self.assertTrue(Permission.objects.filter(codename="approve_checklisttemplate").exists())
