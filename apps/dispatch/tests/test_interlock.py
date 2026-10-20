from datetime import timedelta

from django.db.models import QuerySet

from apps.core.models import AuditLog
from apps.inspections.models import Inspection, InspectionStatus
from apps.production.models import JobTraveler, TravelerOperation
from apps.review.models import QuarantineRecord
from apps.review.tests.helpers import pdi_pass
from apps.dispatch import services
from apps.dispatch.exceptions import ShippingInterlock

from .helpers import (
    AFTER, LATER, DispatchTestCase, approved_lot, conceded_lot, force_pdi_pending, frozen_lot, make_red,
    quarantined_lot, set_hold, set_traveler_status,
)


def check(result, code):
    return next(c for c in result.checks if c.code == code)


class ThreeRulesTests(DispatchTestCase):
    def test_approved_lot_is_green_on_all_three_rules(self):
        lot = approved_lot(self)
        result = services.clearance(lot, AFTER)
        self.assertTrue(result.cleared, result.problems)
        self.assertEqual([c.code for c in result.checks], ["OPERATIONS", "HOLDS", "FINAL_PDI"])
        self.assertTrue(all(c.ok for c in result.checks))

    def test_conceded_lot_is_green(self):
        lot = conceded_lot(self)
        self.assertTrue(services.clearance(lot, AFTER).cleared)

    def test_lot_before_final_pdi_is_locked_on_rules_one_and_three(self):
        pdi_pass(self)  # PDI submitted, not decided
        lot = JobTraveler.objects.get()
        result = services.clearance(lot, AFTER)
        self.assertFalse(result.cleared)
        self.assertFalse(check(result, "OPERATIONS").ok)
        self.assertIn("Op 40", " ".join(check(result, "OPERATIONS").problems))
        self.assertIn("awaiting supervisor approval", " ".join(check(result, "FINAL_PDI").problems))

    def test_fresh_traveler_lists_every_unfinished_operation(self):
        from apps.production import services as production
        from apps.production.tests.helpers import make_work_order
        from apps.inspections.tests.helpers import build_part
        part = build_part(self.engineer, self.manager, number="P-777")
        traveler = production.release_work_order(make_work_order(self.lead, part, number="WO-777", quantity=2), self.lead)
        problems = check(services.clearance(traveler, AFTER), "OPERATIONS").problems
        self.assertEqual(len(problems), 4)
        self.assertIn("Op 10", problems[0])

    def test_skipped_intermediate_operation_locks_the_lot(self):
        lot = approved_lot(self)
        make_red(lot, 20)
        result = services.clearance(lot, AFTER)
        self.assertFalse(result.cleared)
        self.assertIn("Op 20 Heat treatment is pending", check(result, "OPERATIONS").problems[0])

    def test_operation_without_signoff_locks_the_lot(self):
        lot = approved_lot(self)
        QuerySet.update(TravelerOperation.objects.filter(traveler=lot, routing_operation__op_number=30), signed_off_by=None)
        result = services.clearance(lot, AFTER)
        self.assertFalse(check(result, "OPERATIONS").ok)
        self.assertIn("no sign-off", check(result, "OPERATIONS").problems[0])

    def test_running_hold_locks_until_it_expires(self):
        lot = approved_lot(self)
        set_hold(lot, AFTER + timedelta(minutes=90))
        locked = services.clearance(lot, AFTER)
        self.assertFalse(check(locked, "HOLDS").ok)
        self.assertIn("1 hr 30 min", check(locked, "HOLDS").problems[0])
        self.assertTrue(services.clearance(lot, AFTER + timedelta(minutes=91)).cleared)

    def test_quarantined_lot_is_locked_by_the_real_review_flow(self):
        lot = quarantined_lot(self)
        result = services.clearance(lot, AFTER)
        self.assertFalse(result.cleared)
        self.assertIn("Digital Rejection Holding Area", " ".join(check(result, "HOLDS").problems))

    def test_open_quarantine_record_alone_locks_the_lot(self):
        lot = approved_lot(self)
        pdi = Inspection.objects.get(traveler=lot, checklist_type="PDI")
        QuarantineRecord.objects.create(traveler=lot, inspection=pdi, reason="found cracks", created_by=self.supervisor)
        result = services.clearance(lot, AFTER)
        self.assertFalse(check(result, "HOLDS").ok)

    def test_quarantined_status_alone_locks_the_lot(self):
        lot = approved_lot(self)
        set_traveler_status(lot, JobTraveler.Status.QUARANTINED)  # no QuarantineRecord row
        self.assertFalse(check(services.clearance(lot, AFTER), "HOLDS").ok)

    def test_closed_traveler_cannot_ship(self):
        lot = approved_lot(self)
        set_traveler_status(lot, JobTraveler.Status.CLOSED)
        result = services.clearance(lot, AFTER)
        self.assertIn("closed by an MRB disposition", " ".join(check(result, "HOLDS").problems))

    def test_ctq_frozen_lot_is_locked(self):
        lot = frozen_lot(self)
        result = services.clearance(lot, AFTER)
        self.assertFalse(result.cleared)
        self.assertIn("awaiting supervisor approval", " ".join(check(result, "FINAL_PDI").problems))

    def test_pdi_in_progress_is_reported_as_such(self):
        from apps.review.tests.helpers import _begin
        traveler, _ = _begin(self)
        problems = check(services.clearance(traveler, AFTER), "FINAL_PDI").problems
        self.assertIn("still in progress", problems[0])

    def test_rejected_pdi_is_reported(self):
        lot = quarantined_lot(self)
        self.assertIn("rejected", " ".join(check(services.clearance(lot, AFTER), "FINAL_PDI").problems))

    def test_pdi_step_done_but_inspection_not_approved_stays_locked(self):
        lot = approved_lot(self)
        force_pdi_pending(lot, InspectionStatus.SUBMITTED)
        result = services.clearance(lot, AFTER)
        self.assertFalse(check(result, "FINAL_PDI").ok)
        self.assertIn("awaiting supervisor approval", check(result, "FINAL_PDI").problems[0])

    def test_approved_status_without_a_decision_record_does_not_count(self):
        pdi_pass(self)
        lot = JobTraveler.objects.get()
        QuerySet.update(TravelerOperation.objects.filter(traveler=lot, routing_operation__op_number=40),
                        status=TravelerOperation.Status.COMPLETED)
        force_pdi_pending(lot, InspectionStatus.APPROVED)  # status flipped, no SupervisorDecision exists
        self.assertIsNone(services.approved_final_pdi(lot))
        problems = check(services.clearance(lot, AFTER), "FINAL_PDI").problems
        self.assertIn("No approved final PDI inspection", problems[0])

    def test_final_pdi_step_not_completed_is_locked_even_if_inspection_approved(self):
        lot = approved_lot(self)
        make_red(lot, 40)
        result = services.clearance(lot, AFTER)
        self.assertFalse(check(result, "FINAL_PDI").ok)
        self.assertFalse(check(result, "OPERATIONS").ok)


class AssertClearedTests(DispatchTestCase):
    def test_green_returns_the_clearance(self):
        lot = approved_lot(self)
        self.assertTrue(services.assert_cleared(lot, self.clerk, "ship", AFTER).cleared)
        self.assertFalse(AuditLog.objects.filter(field_name="shipping_interlock").exists())

    def test_red_raises_with_every_problem_and_audits_the_attempt(self):
        lot = approved_lot(self)
        make_red(lot, 20)
        set_hold(lot, AFTER + timedelta(hours=2), op_number=30)
        with self.assertRaises(ShippingInterlock) as ctx:
            services.assert_cleared(lot, self.clerk, "issue a dispatch gate pass", AFTER)
        self.assertGreaterEqual(len(ctx.exception.problems), 2)
        row = AuditLog.objects.get(field_name="shipping_interlock")
        self.assertEqual(row.new_value, "BLOCKED")
        self.assertEqual(row.user, self.clerk)
        self.assertEqual(row.object_id, str(lot.pk))
        self.assertIn("issue a dispatch gate pass", row.remarks)
        self.assertIn("Op 20", row.remarks)
