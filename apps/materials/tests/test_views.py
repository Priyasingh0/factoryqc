from django.urls import reverse

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.core.models import AuditLog
from apps.inspections.tests.helpers import build_part
from apps.materials import services
from apps.materials.models import SNCR, IntakeStatus, RawMaterialIntake
from apps.production import services as production
from apps.production.tests.helpers import make_work_order

from .base import MaterialsTestCase
from .helpers import LATER, bad_readings, good_readings, make_supplier, pdf


class ViewBase(MaterialsTestCase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def form(self, **kw):
        data = dict(supplier=self.supplier.pk, po_number="PO-9", grade=self.grade.pk, heat_number="H-5000",
                    mtc_heat_number="H-5000", quantity="250", unit="KG", mtc_file=pdf())
        data.update(kw)
        return data


class AccessTests(ViewBase):
    def test_anonymous_sent_to_login(self):
        for name in ("list", "new", "sncr_list", "suppliers"):
            r = self.client.get(reverse(f"materials:{name}"))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("login", r["Location"])

    def test_roles_without_access_get_403(self):
        for user in (self.operator, self.sup):
            self.login(user)
            self.assertEqual(self.client.get(reverse("materials:list")).status_code, 403, user.role)

    def test_viewers_cannot_log_intake(self):
        for user in (self.qm, self.lead, self.buyer):
            self.login(user)
            self.assertEqual(self.client.get(reverse("materials:new")).status_code, 403, user.role)
            self.assertEqual(self.client.post(reverse("materials:new"), self.form()).status_code, 403)
        self.assertFalse(RawMaterialIntake.objects.exists())

    def test_nav_link_follows_permission(self):
        self.login(self.dock)
        self.assertContains(self.client.get(reverse("accounts:home")), 'href="/materials/"')
        self.login(self.operator)
        self.assertNotContains(self.client.get(reverse("accounts:home")), 'href="/materials/"')

    def test_workflow_posts_reject_get_and_wrong_roles(self):
        i = self.receive()
        owner = {"review": self.eng, "lab": self.eng, "reject": self.dock, "recall": self.qh, "resolve": self.buyer}
        for name, user in owner.items():
            self.login(user)
            self.assertEqual(self.client.get(reverse(f"materials:{name}", args=[i.pk])).status_code, 405, name)
        self.login(self.lead)
        for name in ("review", "lab", "reject", "recall", "resolve"):
            self.assertEqual(self.client.post(reverse(f"materials:{name}", args=[i.pk])).status_code, 403, name)


class IntakeFlowTests(ViewBase):
    def test_dock_clerk_logs_intake_with_an_mtc_upload(self):
        self.login(self.dock)
        page = self.client.get(reverse("materials:new"))
        self.assertContains(page, 'type="file"')
        self.assertContains(page, "capture")
        r = self.client.post(reverse("materials:new"), self.form())
        intake = RawMaterialIntake.objects.get()
        self.assertRedirects(r, reverse("materials:detail", args=[intake.pk]))
        self.assertEqual(intake.received_by, self.dock)

    def test_missing_mtc_or_heat_mismatch_is_shown_and_nothing_saved(self):
        self.login(self.dock)
        r = self.client.post(reverse("materials:new"), self.form(mtc_file=""))
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("materials:new"), self.form(mtc_heat_number="H-9"), follow=True)
        self.assertContains(r, "does not match")
        self.assertFalse(RawMaterialIntake.objects.exists())

    def test_full_review_to_clearance_through_the_views(self):
        self.login(self.dock)
        self.client.post(reverse("materials:new"), self.form())
        intake = RawMaterialIntake.objects.get()
        self.login(self.eng)
        page = self.client.get(reverse("materials:detail", args=[intake.pk]))
        self.assertContains(page, "Verify MTC")
        data = {f"prop_{k}": v for k, v in good_readings(self.grade).items()}
        r = self.client.post(reverse("materials:review", args=[intake.pk]), data, follow=True)
        self.assertContains(r, "cleared for production")
        intake.refresh_from_db()
        self.assertEqual(intake.status, IntakeStatus.CLEARED)
        self.assertContains(self.client.get(reverse("materials:label", args=[intake.pk])), intake.barcode)

    def test_out_of_spec_mtc_shows_quarantine_message(self):
        intake = self.receive()
        self.login(self.eng)
        data = {f"prop_{k}": v for k, v in bad_readings(self.grade).items()}
        r = self.client.post(reverse("materials:review", args=[intake.pk]), data, follow=True)
        self.assertContains(r, "Supplier Quarantine")
        self.assertContains(r, "SNCR-")

    def test_receiver_does_not_see_the_review_form(self):
        intake = self.receive()
        self.login(self.dock)
        self.assertNotContains(self.client.get(reverse("materials:detail", args=[intake.pk])), "Verify MTC")

    def test_lab_flow_and_sample_tag(self):
        intake = self.in_lab()
        self.login(self.eng)
        self.assertContains(self.client.get(reverse("materials:label", args=[intake.pk])), intake.sample_tag)
        r = self.client.post(reverse("materials:lab", args=[intake.pk]),
                             {"result": "pass", "report_reference": "LAB-5"}, follow=True)
        self.assertContains(r, "Lab result recorded")
        intake.refresh_from_db()
        self.assertEqual(intake.status, IntakeStatus.CLEARED)

    def test_barcode_label_is_locked_until_cleared(self):
        intake = self.receive()
        self.login(self.dock)
        r = self.client.get(reverse("materials:label", args=[intake.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "Label locked", status_code=403)
        lab = self.in_lab(heat="H-LAB")
        self.assertNotContains(self.client.get(reverse("materials:label", args=[lab.pk])), lab.barcode)

    def test_reject_button_creates_the_sncr(self):
        intake = self.receive()
        self.login(self.dock)
        r = self.client.post(reverse("materials:reject", args=[intake.pk]),
                             {"reason_code": "DIMENSION", "details": "Bars are bent beyond tolerance"}, follow=True)
        self.assertContains(r, "Supplier Quarantine")
        self.assertContains(r, "SUPPLIER QUARANTINE")
        self.assertEqual(SNCR.objects.count(), 1)
        self.login(self.buyer)
        page = self.client.get(reverse("materials:detail", args=[intake.pk]))
        self.assertContains(page, "Close SNCR")
        self.client.post(reverse("materials:resolve", args=[intake.pk]), {"resolution": "RTV", "reference": "DN-1"})
        intake.refresh_from_db()
        self.assertEqual(intake.status, IntakeStatus.RETURNED)

    def test_bad_form_shows_an_error_message_and_changes_nothing(self):
        intake = self.receive()
        self.login(self.dock)
        r = self.client.post(reverse("materials:reject", args=[intake.pk]), {"reason_code": "", "details": ""}, follow=True)
        self.assertContains(r, "Select a reason")
        intake.refresh_from_db()
        self.assertEqual(intake.status, IntakeStatus.RECEIVED)

    def test_recall_button_for_quality_head(self):
        intake = self.cleared()
        self.login(self.qh)
        self.assertContains(self.client.get(reverse("materials:detail", args=[intake.pk])), "Quarantine this cleared lot")
        self.client.post(reverse("materials:recall", args=[intake.pk]), {"details": "Supplier advised defective chemistry"})
        intake.refresh_from_db()
        self.assertEqual(intake.status, IntakeStatus.SUPPLIER_QUARANTINE)

    def test_list_filter_and_pages(self):
        a, b = self.receive(heat="H-A"), self.quarantined(heat="H-B")
        self.login(self.qm)
        page = self.client.get(reverse("materials:list"), {"status": "SUPPLIER_QUARANTINE"})
        self.assertContains(page, b.lot_number)
        self.assertNotContains(page, a.lot_number)
        self.assertEqual(self.client.get(reverse("materials:list"), {"status": "junk"}).status_code, 200)
        self.assertContains(self.client.get(reverse("materials:sncr_list")), "SNCR-")
        self.assertContains(self.client.get(reverse("materials:suppliers")), self.supplier.name)

    def test_mtc_file_is_served_safely(self):
        intake = self.receive()
        self.login(self.eng)
        r = self.client.get(reverse("materials:mtc_file", args=[intake.pk]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.login(self.operator)
        self.assertEqual(self.client.get(reverse("materials:mtc_file", args=[intake.pk])).status_code, 403)


class IssueViewTests(ViewBase):
    def setUp(self):
        super().setUp()
        part = build_part(self.eng, self.qm, number="P-VIEW")
        self.wo = make_work_order(self.lead, part, number="WO-V", quantity=2, heat="H-1001", issue_material=False)
        self.lot = self.cleared()
        self.login(self.lead)

    def test_only_cleared_lots_of_the_right_heat_are_offered(self):
        self.receive(heat="H-1001", po="PO-RAW")  # received, not cleared
        self.cleared(heat="H-OTHER", po="PO-X")
        page = self.client.get(reverse("materials:issue", args=[self.wo.pk]))
        self.assertContains(page, self.lot.lot_number)
        self.assertEqual(page.content.decode().count("RM-0"), 1)

    def test_issue_from_the_list_and_by_barcode(self):
        r = self.client.post(reverse("materials:issue", args=[self.wo.pk]), {"intake": self.lot.pk, "quantity": "50"})
        self.assertRedirects(r, reverse("production:work_order_detail", args=[self.wo.pk]))
        self.assertContains(self.client.get(r["Location"]), self.lot.lot_number)
        part = build_part(self.eng, self.qm, number="P-VIEW2")
        wo2 = make_work_order(self.lead, part, number="WO-V2", quantity=2, heat="H-1001", issue_material=False)
        r = self.client.post(reverse("materials:issue", args=[wo2.pk]), {"lot_barcode": self.lot.barcode, "quantity": "20"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.lot.issues.count(), 2)

    def test_quarantined_barcode_is_refused_and_audited(self):
        services.recall_lot(self.lot, self.qh, "Supplier advised heat has defective chemistry", LATER)
        r = self.client.post(reverse("materials:issue", args=[self.wo.pk]),
                             {"lot_barcode": self.lot.barcode, "quantity": "20"}, follow=True)
        self.assertContains(r, "cannot be issued to production")
        self.assertFalse(self.lot.issues.exists())
        self.assertTrue(AuditLog.objects.filter(field_name="issue_blocked").exists())

    def test_neither_lot_nor_barcode(self):
        r = self.client.post(reverse("materials:issue", args=[self.wo.pk]), {"quantity": "20"})
        self.assertContains(r, "Choose a cleared lot")

    def test_other_roles_cannot_issue(self):
        self.login(self.qm)
        self.assertEqual(self.client.get(reverse("materials:issue", args=[self.wo.pk])).status_code, 403)

    def test_work_order_page_shows_the_material_and_the_release_blocker(self):
        page = self.client.get(reverse("production:work_order_detail", args=[self.wo.pk]))
        self.assertContains(page, "Issue raw material")
        self.assertContains(page, "before release")
        services.issue_material(self.wo, self.lot, "5", self.lead, LATER)
        page = self.client.get(reverse("production:work_order_detail", args=[self.wo.pk]))
        self.assertContains(page, self.lot.lot_number)
        self.assertNotContains(page, "Not ready to release")

    def test_trace_page_lists_supplier_lots_for_a_heat(self):
        r = self.client.get(reverse("production:trace"), {"kind": "heat", "q": "H-1001"})
        self.assertContains(r, "Supplier lots with this heat number")
        self.assertContains(r, self.lot.lot_number)
