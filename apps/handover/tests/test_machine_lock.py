from datetime import timedelta
from unittest import mock

from apps.core.models import AuditLog, Notification
from apps.handover import services
from apps.handover.exceptions import HandoverPending
from apps.plant.services import record_pm_check
from apps.plant.tests.helpers import at, make_machine
from apps.production import services as production
from apps.production.models import ProductionRun, TravelerOperation
from apps.production.tests.helpers import NOW, op_at, run_through

from .base import EVENING, HANDOVER_AT, NEXT_MORNING, HandoverTestCase


class MachineLockTests(HandoverTestCase):
    """PRD 12.2: incoming workers must acknowledge the handover before starting work."""

    def test_no_handover_filed_means_no_lock(self):
        _, machine, op = self.running_job()
        self.ready_for_evening(machine)
        self.assertEqual(services.machine_problem(machine, EVENING), "")
        production.record_parts(op, self.operator2, 1, EVENING)

    def test_unsigned_handover_locks_the_machine_for_the_next_shift(self):
        _, machine, op = self.running_job()
        self.hand_over(machine)
        self.ready_for_evening(machine)
        with self.assertRaisesMessage(HandoverPending, "not been countersigned"):
            production.record_parts(op, self.operator2, 1, EVENING)

    def test_signed_but_unacknowledged_handover_still_locks(self):
        _, machine, op = self.running_job()
        services.sign_handover(self.hand_over(machine), self.sup, "", HANDOVER_AT)
        self.ready_for_evening(machine)
        with self.assertRaisesMessage(HandoverPending, "acknowledge"):
            production.record_parts(op, self.operator2, 1, EVENING)

    def test_acknowledgement_unlocks_counting_and_finishing(self):
        _, machine, op = self.running_job(parts=2)
        self.ready_for_evening(machine)
        self.signed(machine)
        with self.assertRaises(HandoverPending):
            production.complete_operation(op, self.operator2, EVENING)
        services.acknowledge_handover(machine.shift_handovers.get(), self.operator2, "", EVENING)
        production.record_parts(op, self.operator2, 3, EVENING)
        production.complete_operation(op, self.operator2, EVENING + timedelta(minutes=5))
        op.refresh_from_db()
        self.assertEqual(op.status, TravelerOperation.Status.COMPLETED)

    def test_starting_a_new_operation_is_locked_too(self):
        traveler, machine, op = self.running_job(parts=5)
        production.complete_operation(op, self.operator, NOW + timedelta(hours=1))
        self.signed(machine)
        self.ready_for_evening(machine)
        next_op = op_at(traveler, 20)
        with self.assertRaises(HandoverPending):
            production.start_operation(next_op, self.operator2, machine, EVENING)
        services.acknowledge_handover(machine.shift_handovers.get(), self.operator2, "", EVENING)
        production.start_operation(next_op, self.operator2, machine, EVENING)
        next_op.refresh_from_db()
        self.assertEqual(next_op.status, TravelerOperation.Status.IN_PROGRESS)

    def test_the_same_shift_is_not_locked(self):
        _, machine, op = self.running_job(parts=2)
        self.hand_over(machine)
        production.record_parts(op, self.operator, 1, HANDOVER_AT + timedelta(minutes=10))

    def test_other_machines_are_unaffected(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine)
        other = make_machine("CNC-99")
        self.assertEqual(services.machine_problem(other, EVENING), "")

    def test_only_the_latest_earlier_handover_counts(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine)                                       # morning, never acknowledged
        evening = self.hand_over(machine, now=EVENING, job_status_notes="Evening: job still running fine.")
        services.sign_handover(evening, self.sup, "", EVENING)
        services.acknowledge_handover(evening, self.operator2, "", EVENING + timedelta(hours=7))
        self.assertEqual(services.machine_problem(machine, at(2026, 3, 11, 0, 30)), "")
        # during the evening itself the unacknowledged morning handover is still the one that matters
        self.assertIn("Morning shift", services.machine_problem(machine, at(2026, 3, 10, 21, 0)))

    def test_night_shift_belongs_to_the_day_it_started(self):
        _, machine, _ = self.running_job()
        night = at(2026, 3, 10, 23, 0)
        self.hand_over(machine, now=night, job_status_notes="Night shift: job running fine.")
        self.assertEqual(services.machine_problem(machine, at(2026, 3, 11, 3, 0)), "")  # still the same night shift
        self.assertIn("Night shift of 10 Mar", services.machine_problem(machine, NEXT_MORNING))

    def test_night_shift_comes_after_the_evening_shift_of_the_same_day(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine, now=EVENING)
        self.assertIn("Evening shift of 10 Mar", services.machine_problem(machine, at(2026, 3, 10, 23, 0)))
        self.assertEqual(services.machine_problem(machine, EVENING + timedelta(hours=1)), "")

    def test_blocked_attempt_is_audited_and_supervisors_alerted(self):
        _, machine, op = self.running_job()
        self.hand_over(machine)
        self.ready_for_evening(machine)
        with self.assertRaises(HandoverPending):
            production.record_parts(op, self.operator2, 1, EVENING)
        row = AuditLog.objects.get(field_name="shift_handover", new_value="BLOCKED")
        self.assertEqual(row.user, self.operator2)
        self.assertIn("record produced parts", row.remarks)
        self.assertIn("CNC-04", row.remarks)
        self.assertTrue(Notification.objects.filter(
            recipient_role="SHIFT_SUPERVISOR", subject__startswith="Work blocked by pending handover").exists())
        self.assertEqual(ProductionRun.objects.get(traveler_operation=op).parts_produced, 2)

    def test_blocked_attempt_to_start_is_audited_before_anything_changes(self):
        traveler, machine, op = self.running_job(parts=5)
        production.complete_operation(op, self.operator, NOW + timedelta(hours=1))
        self.hand_over(machine, job_status_notes="")
        self.ready_for_evening(machine)
        next_op = op_at(traveler, 20)
        with self.assertRaises(HandoverPending):
            production.start_operation(next_op, self.operator2, machine, EVENING)
        next_op.refresh_from_db()
        self.assertEqual(next_op.status, TravelerOperation.Status.PENDING)
        self.assertTrue(AuditLog.objects.filter(field_name="shift_handover", remarks__contains="start an operation").exists())

    def test_permission_is_checked_before_the_handover_lock(self):
        _, machine, op = self.running_job()
        self.hand_over(machine)
        before = AuditLog.objects.filter(field_name="shift_handover").count()
        with self.assertRaises(production.NotAuthorized):
            production.record_parts(op, self.qa, 1, EVENING)
        self.assertEqual(AuditLog.objects.filter(field_name="shift_handover").count(), before)

    def test_inner_checks_hold_even_if_the_outer_gate_is_skipped(self):
        _, machine, op = self.running_job(parts=2)
        self.ready_for_evening(machine)
        self.hand_over(machine)
        noop = mock.patch.object(production.handover, "assert_machine_clear", lambda *a, **k: None)
        with noop:
            with self.assertRaises(HandoverPending):
                production.record_parts(op, self.operator2, 1, EVENING)
            with self.assertRaises(HandoverPending):
                production.complete_operation(op, self.operator2, EVENING)
        self.assertEqual(ProductionRun.objects.get(traveler_operation=op).parts_produced, 2)

    def test_inner_check_also_guards_start(self):
        traveler, machine, op = self.running_job(parts=5)
        production.complete_operation(op, self.operator, NOW + timedelta(hours=1))
        self.hand_over(machine, job_status_notes="")
        self.ready_for_evening(machine)
        with mock.patch.object(production.handover, "assert_machine_clear", lambda *a, **k: None):
            with self.assertRaises(HandoverPending):
                production.start_operation(op_at(traveler, 20), self.operator2, machine, EVENING)

    def test_scan_and_start_goes_through_the_same_lock(self):
        traveler, machine, op = self.running_job(parts=5)
        production.complete_operation(op, self.operator, NOW + timedelta(hours=1))
        self.hand_over(machine, job_status_notes="")
        self.ready_for_evening(machine)
        with self.assertRaises(HandoverPending):
            production.scan_and_start(op_at(traveler, 20), self.operator2, machine.qr_token, traveler.barcode, EVENING)

    def test_a_lock_message_names_the_machine_and_the_shift(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine)
        message = services.machine_problem(machine, EVENING)
        self.assertIn("CNC-04", message)
        self.assertIn("Morning shift of 10 Mar 2026", message)

    def test_pending_handovers_lists_only_unacknowledged_ones(self):
        _, machine, _ = self.running_job()
        done = self.acknowledged(machine)
        other = make_machine("CNC-12")
        waiting = self.hand_over(other, job_status_notes="")
        self.assertEqual(services.pending_handovers(), [waiting])
        self.assertNotIn(done, services.pending_handovers())


class MissingHandoverTests(HandoverTestCase):
    def test_a_job_left_running_without_a_handover_is_flagged_in_the_next_shift(self):
        _, machine, _ = self.running_job()
        message = services.missing_handover(machine, EVENING)
        self.assertIn("Morning shift of 10 Mar 2026", message)
        self.assertIn("CNC-04", message)

    def test_flag_clears_once_the_handover_is_filed(self):
        _, machine, _ = self.running_job()
        self.hand_over(machine)
        self.assertEqual(services.missing_handover(machine, EVENING), "")

    def test_nothing_is_flagged_during_the_shift_the_job_started_in(self):
        _, machine, _ = self.running_job()
        self.assertEqual(services.missing_handover(machine, HANDOVER_AT), "")

    def test_an_idle_machine_is_never_flagged(self):
        self.assertEqual(services.missing_handover(make_machine("CNC-31"), EVENING), "")

    def test_the_flag_does_not_lock_the_machine(self):
        _, machine, op = self.running_job()
        self.ready_for_evening(machine)
        production.record_parts(op, self.operator2, 1, EVENING)

    def test_board_shows_the_flag(self):
        from django.urls import reverse
        from django.utils import timezone
        from apps.accounts.tests.helpers import TEST_PASSWORD

        _, machine, _ = self.running_job()
        self.client.login(username=self.sup.username, password=TEST_PASSWORD)
        with mock.patch.object(timezone, "now", return_value=EVENING):
            page = self.client.get(reverse("handover:board"))
        self.assertContains(page, "Handover missing")
