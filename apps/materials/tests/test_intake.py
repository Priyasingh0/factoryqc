from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.models import AuditLog
from apps.core.models import Notification
from apps.materials import services
from apps.materials.exceptions import IntakeRejected, NotAuthorized
from apps.materials.models import IntakeStatus, RawMaterialIntake

from .base import MaterialsTestCase
from .helpers import LATER, make_grade, make_supplier, pdf


class CreateIntakeTests(MaterialsTestCase):
    def test_dock_clerk_logs_a_delivery(self):
        i = self.receive()
        self.assertEqual(i.status, IntakeStatus.RECEIVED)
        self.assertEqual(i.heat_number, "H-1001")
        self.assertEqual(i.received_by, self.dock)
        self.assertEqual(i.quantity, Decimal("500"))
        self.assertEqual(i.lot_number, f"RM-{i.pk:06d}")
        self.assertTrue(i.mtc_file.name.endswith(".pdf"))
        self.assertEqual(len(i.mtc_sha256), 64)
        self.assertFalse(i.lab_required)
        row = AuditLog.objects.get(action=AuditLog.Action.CREATE, model_name="materials.RawMaterialIntake")
        self.assertEqual(row.user, self.dock)
        self.assertIn("H-1001", row.remarks)

    def test_stored_under_a_random_name_not_the_clients(self):
        i = self.receive(mtc_upload=pdf("../../evil name.pdf"))
        self.assertNotIn("evil", i.mtc_file.name)

    def test_heat_number_is_normalised(self):
        i = self.receive(heat=" h-1001 ", mtc_heat_number="H-1001")
        self.assertEqual(i.heat_number, "H-1001")

    def test_mtc_is_mandatory(self):
        with self.assertRaises(IntakeRejected):
            self.receive(mtc_upload=None)
        self.assertFalse(RawMaterialIntake.objects.exists())

    def test_empty_unreadable_and_oversized_mtc_files_are_refused(self):
        for data in (b"", b"just some text", b"%PDF-" + b"0" * (10 * 1024 * 1024)):
            with self.assertRaises(IntakeRejected):
                self.receive(mtc_upload=SimpleUploadedFile("x.pdf", data))
        self.assertFalse(RawMaterialIntake.objects.exists())

    def test_jpeg_and_png_scans_are_accepted(self):
        self.receive(heat="H-1", mtc_upload=SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0" + b"0" * 20))
        self.receive(heat="H-2", mtc_upload=SimpleUploadedFile("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20))
        self.assertEqual(RawMaterialIntake.objects.count(), 2)

    def test_heat_number_on_the_mtc_must_match(self):
        with self.assertRaises(IntakeRejected) as ctx:
            self.receive(heat="H-1001", mtc_heat_number="H-9999")
        self.assertIn("does not match", str(ctx.exception))
        self.assertFalse(RawMaterialIntake.objects.exists())

    def test_matching_ignores_case_and_spaces(self):
        self.receive(heat="h 1001", mtc_heat_number="H-1001".replace("-", ""))  # H1001 vs H1001
        self.assertEqual(RawMaterialIntake.objects.get().heat_number, "H1001")

    def test_bad_heat_numbers_are_refused(self):
        for heat in ("", " ", "H", "<script>", "a" * 60):
            with self.assertRaises(IntakeRejected):
                self.receive(heat=heat, mtc_heat_number=heat)

    def test_quantity_must_be_positive_number(self):
        for qty in ("0", "-5", "abc", "NaN", "Infinity"):
            with self.assertRaises(IntakeRejected, msg=qty):
                self.receive(qty=qty)

    def test_po_is_required(self):
        with self.assertRaises(IntakeRejected):
            self.receive(po="  ")

    def test_inactive_supplier_or_grade_refused(self):
        dead = make_supplier("SUP-X", "Closed Mills", active=False)
        with self.assertRaises(IntakeRejected):
            self.receive(supplier=dead)
        grade = make_grade("OLD")
        grade.is_active = False
        grade.save()
        with self.assertRaises(IntakeRejected):
            self.receive(grade=grade)

    def test_same_heat_and_po_cannot_be_received_twice(self):
        self.receive()
        with self.assertRaises(IntakeRejected):
            self.receive(po="po-77")
        self.receive(po="PO-78")  # a different PO is a different delivery

    def test_only_dock_clerks_log_intake(self):
        for user in (self.eng, self.qm, self.lead, self.operator, self.buyer):
            with self.assertRaises(NotAuthorized):
                self.receive(user=user)
        self.assertFalse(RawMaterialIntake.objects.exists())

    def test_grade_lab_flag_is_copied_at_receipt(self):
        self.assertTrue(self.receive(grade=self.lab_grade).lab_required)

    def test_quality_is_alerted_to_review_the_mtc(self):
        i = self.receive()
        subjects = Notification.objects.filter(related_model="materials.RawMaterialIntake", related_object_id=str(i.pk)).values_list("recipient_role", flat=True)
        self.assertEqual(set(subjects), {"QUALITY_MANAGER", "QUALITY_ENGINEER"})
