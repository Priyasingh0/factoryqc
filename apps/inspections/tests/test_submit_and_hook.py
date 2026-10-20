from datetime import timedelta

from apps.checklists.models import ChecklistType
from apps.core.models import AuditLog, Notification
from apps.inspections import services
from apps.inspections.exceptions import InspectionLocked, NotAuthorized, SubmitBlocked
from apps.inspections.models import DefectPhoto, Inspection, InspectionStatus, Measurement
from apps.production import services as production
from apps.production.exceptions import OperationBlocked
from apps.production.models import ProductionRun, TravelerOperation

from .helpers import (
    NOW, InspectionTestCase, begin_first_piece, enter, enter_good_first_piece, field_named,
    lot_in_setup, tiny_png,
)
from .test_photos_and_freeze import PdiCase, upload


class SubmitTests(PdiCase):
    def test_incomplete_checklist_cannot_be_submitted(self):
        enter(self, self.insp, "Shaft diameter", "25")
        with self.assertRaises(SubmitBlocked) as ctx:
            services.submit_inspection(self.insp, self.qa, now=NOW)
        message = str(ctx.exception)
        for label in ("Bore", "Roughness Ra", "Coating applied"):
            self.assertIn(label, message)
        self.assertNotIn("Shaft diameter", message)

    def test_empty_checklist_cannot_be_submitted(self):
        with self.assertRaises(SubmitBlocked):
            services.submit_inspection(self.insp, self.qa, now=NOW)

    def test_complete_checklist_submits_and_locks(self):
        self.good_rest()
        done = services.submit_inspection(self.insp, self.qa, remarks=" Checked twice ", now=NOW)
        self.assertEqual(done.status, InspectionStatus.SUBMITTED)
        self.assertEqual(done.submitted_at, NOW)
        self.assertEqual(done.remarks, "Checked twice")
        self.assertTrue(done.all_within_tolerance)

    def test_submission_alerts_shift_supervisor_and_is_audited(self):
        self.good_rest()
        services.submit_inspection(self.insp, self.qa, now=NOW)
        self.assertTrue(Notification.objects.filter(
            recipient=self.supervisor, subject__startswith="Awaiting approval").exists())
        row = AuditLog.objects.get(action=AuditLog.Action.SIGN_OFF, model_name="inspections.Inspection")
        self.assertEqual((row.previous_value, row.new_value), ("IN_PROGRESS", "SUBMITTED"))

    def test_only_owner_inspector_submits(self):
        self.good_rest()
        for user in (self.qa2, self.supervisor, self.operator):
            with self.assertRaises(NotAuthorized):
                services.submit_inspection(self.insp, user, now=NOW)

    def test_cannot_submit_twice(self):
        self.good_rest()
        services.submit_inspection(self.insp, self.qa, now=NOW)
        with self.assertRaises(InspectionLocked):
            services.submit_inspection(self.insp, self.qa, now=NOW)

    def test_inspector_is_free_to_start_another_after_submitting(self):
        self.good_rest()
        services.submit_inspection(self.insp, self.qa, now=NOW)
        self.assertFalse(Inspection.objects.filter(inspector=self.qa, status="IN_PROGRESS").exists())


class ImmutabilityTests(PdiCase):
    """PRD 13.3: past inspection data cannot be altered or deleted at any level."""

    def setUp(self):
        super().setUp()
        self.good_rest(skip=("Roughness Ra",))
        self.bad = enter(self, self.insp, "Roughness Ra", "0.9", gauge="RT-9")
        self.photo(self.bad, 2)
        services.submit_inspection(self.insp, self.qa, now=NOW)
        self.insp.refresh_from_db()
        self.photo_row = self.bad.photos.first()

    def test_submitted_inspection_cannot_be_edited(self):
        self.insp.remarks = "rewritten"
        with self.assertRaises(InspectionLocked):
            self.insp.save()

    def test_submitted_inspection_cannot_change_status_directly(self):
        self.insp.status = InspectionStatus.IN_PROGRESS
        with self.assertRaises(InspectionLocked):
            self.insp.save()

    def test_measurement_cannot_be_edited_to_pass(self):
        m = Measurement.objects.get(pk=self.bad.pk)
        m.value_numeric = 0.5
        with self.assertRaises(InspectionLocked):
            m.save()

    def test_nothing_can_be_deleted(self):
        for obj in (self.insp, self.bad, self.photo_row):
            with self.assertRaises(InspectionLocked):
                obj.delete()

    def test_bulk_update_and_delete_blocked(self):
        for qs in (Inspection.objects.all(), Measurement.objects.all(), DefectPhoto.objects.all()):
            with self.assertRaises(InspectionLocked):
                qs.update(id=1)
            with self.assertRaises(InspectionLocked):
                qs.delete()
        with self.assertRaises(InspectionLocked):
            Measurement.objects.bulk_update([self.bad], ["value_numeric"])

    def test_cannot_add_reading_or_photo_to_submitted_inspection(self):
        field = field_named(self.insp.template, "Bore")
        with self.assertRaises(InspectionLocked):
            Measurement.objects.create(
                inspection=self.insp, field=field, value_numeric=10, gauge=self.cal, entered_by=self.qa)
        with self.assertRaises(InspectionLocked):
            DefectPhoto.objects.create(measurement=self.bad, sha256="x" * 64, uploaded_by=self.qa, image="a.png")

    def test_no_change_or_delete_permissions_exist(self):
        from django.contrib.auth.models import Permission
        codes = set(Permission.objects.filter(content_type__app_label="inspections").values_list("codename", flat=True))
        for model in ("inspection", "measurement", "defectphoto"):
            self.assertIn(f"view_{model}", codes)
            for verb in ("add", "change", "delete"):
                self.assertNotIn(f"{verb}_{model}", codes)


class FirstPieceHookTests(InspectionTestCase):
    """PRD 9.1: QA cannot sign off a first piece without a passing submitted inspection."""

    def setUp(self):
        self.traveler, self.machine = lot_in_setup(self)
        self.op = TravelerOperation.objects.get(traveler=self.traveler, routing_operation__op_number=10)

    def run_(self):
        return ProductionRun.objects.get(traveler_operation=self.op)

    def test_signoff_refused_without_any_inspection(self):
        with self.assertRaisesMessage(OperationBlocked, "submitted first-piece inspection is required"):
            production.clear_first_piece(self.run_(), self.qa, NOW)
        self.assertEqual(self.run_().state, ProductionRun.State.SETUP)

    def test_signoff_refused_while_inspection_still_in_progress(self):
        begin_first_piece(self, self.traveler, self.machine)
        with self.assertRaises(OperationBlocked):
            production.clear_first_piece(self.run_(), self.qa, NOW)

    def test_submitting_links_the_inspection_to_the_run(self):
        insp = begin_first_piece(self, self.traveler, self.machine)
        enter_good_first_piece(self, insp)
        services.submit_inspection(insp, self.qa, now=NOW)
        self.assertEqual(self.run_().first_piece_inspection, insp)

    def test_full_flow_unlocks_the_counter(self):
        insp = begin_first_piece(self, self.traveler, self.machine)
        enter_good_first_piece(self, insp)
        services.submit_inspection(insp, self.qa, now=NOW)
        production.clear_first_piece(self.run_(), self.qa2, NOW)
        run = production.record_parts(self.op, self.operator, 1, NOW)
        self.assertEqual(run.parts_produced, 1)

    def test_failed_first_piece_cannot_be_signed_off(self):
        insp = begin_first_piece(self, self.traveler, self.machine)
        enter(self, insp, "Shaft diameter", "26")
        m = insp.measurements.get()
        for i in range(2):
            services.add_defect_photo(m, upload(tiny_png(bytes([i + 1]))), self.qa, NOW)
        enter(self, insp, "Burrs removed", True, gauge="")
        services.submit_inspection(insp, self.qa, now=NOW)
        with self.assertRaisesMessage(OperationBlocked, "out-of-tolerance"):
            production.clear_first_piece(self.run_(), self.qa, NOW)
        self.assertEqual(self.run_().state, ProductionRun.State.SETUP)

    def test_counter_stays_locked_after_a_failed_first_piece(self):
        from apps.production.exceptions import FirstPieceLocked
        insp = begin_first_piece(self, self.traveler, self.machine)
        enter(self, insp, "Shaft diameter", "26")
        with self.assertRaises(FirstPieceLocked):
            production.record_parts(self.op, self.operator, 1, NOW)

    def test_inspect_again_after_a_failed_first_piece(self):
        insp = begin_first_piece(self, self.traveler, self.machine)
        enter(self, insp, "Shaft diameter", "26")
        m = insp.measurements.get()
        for i in range(2):
            services.add_defect_photo(m, upload(tiny_png(bytes([i + 1]))), self.qa, NOW)
        enter(self, insp, "Burrs removed", True, gauge="")
        services.submit_inspection(insp, self.qa, now=NOW)
        retry = begin_first_piece(self, self.traveler, self.machine, now=NOW + timedelta(minutes=20))
        enter_good_first_piece(self, retry)
        services.submit_inspection(retry, self.qa, now=NOW)
        self.assertEqual(self.run_().first_piece_inspection, retry)
        production.clear_first_piece(self.run_(), self.qa2, NOW)

    def test_passing_inspection_blocks_a_duplicate(self):
        from apps.inspections.exceptions import CannotStart
        insp = begin_first_piece(self, self.traveler, self.machine)
        enter_good_first_piece(self, insp)
        services.submit_inspection(insp, self.qa, now=NOW)
        with self.assertRaisesMessage(CannotStart, "already submitted"):
            begin_first_piece(self, self.traveler, self.machine, user=self.qa2)
