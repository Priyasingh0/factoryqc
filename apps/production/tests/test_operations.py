from datetime import timedelta

from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog
from apps.core.signing import QRKind, make_token
from apps.plant.exceptions import MachineNotReady
from apps.plant.tests.helpers import at, make_machine
from apps.production import services
from apps.production.exceptions import (
    FirstPieceLocked, HoldActive, NotAuthorized, OperationBlocked,
)
from apps.production.models import JobTraveler, PartSerial, ProductionRun, TravelerOperation

from .helpers import NOW, ProductionTestCase, make_machine_ready, op_at, released_traveler, run_through, sign_off

OP = TravelerOperation.Status


class SequenceTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self)

    def test_cannot_skip_ahead(self):
        with self.assertRaisesMessage(OperationBlocked, "Op 10"):
            services.start_operation(op_at(self.traveler, 20), self.operator, self.machine, NOW)

    def test_cannot_start_while_previous_step_is_only_in_progress(self):
        services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        other = make_machine_ready("CNC-05", user=self.operator)
        with self.assertRaisesMessage(OperationBlocked, "must be completed"):
            services.start_operation(op_at(self.traveler, 20), self.operator, other, NOW)

    def test_start_records_machine_operator_shift_and_clock_in(self):
        op = services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        self.assertEqual(op.status, OP.IN_PROGRESS)
        self.assertEqual((op.machine, op.operator, op.clock_in), (self.machine, self.operator, NOW))
        self.assertEqual(op.shift, "MORNING")

    def test_cannot_start_twice(self):
        services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        with self.assertRaises(OperationBlocked):
            services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)

    def test_final_pdi_cannot_be_started_here(self):
        with self.assertRaisesMessage(OperationBlocked, "final PDI"):
            services.start_operation(op_at(self.traveler, 40), self.operator, self.machine, NOW)

    def test_only_operators_may_start(self):
        for user in (self.qa, self.lead, self.engineer, self.dock):
            with self.assertRaises(NotAuthorized):
                services.start_operation(op_at(self.traveler, 10), user, self.machine, NOW)

    def test_machine_with_overdue_pm_check_is_locked(self):
        cold = make_machine("CNC-09")
        with self.assertRaises(MachineNotReady):
            services.start_operation(op_at(self.traveler, 10), self.operator, cold, NOW)
        self.assertEqual(op_at(self.traveler, 10).status, OP.PENDING)

    def test_machine_cannot_run_two_jobs(self):
        services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        # a second work order tries to use the same machine
        from .helpers import make_ready_part, make_work_order
        part2 = make_ready_part(self.engineer, self.manager, number="P-201")
        t2 = services.release_work_order(make_work_order(self.lead, part2, number="WO-2"), self.lead)
        with self.assertRaisesMessage(OperationBlocked, "already running"):
            services.start_operation(op_at(t2, 10), self.operator2, self.machine, NOW)

    def test_inactive_machine_refused(self):
        self.machine.is_active = False
        self.machine.save()
        with self.assertRaises(OperationBlocked):
            services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)

    def test_quarantined_traveler_cannot_continue(self):
        JobTraveler.objects.filter(pk=self.traveler.pk).update(status=JobTraveler.Status.QUARANTINED)
        self.traveler.refresh_from_db()
        with self.assertRaisesMessage(OperationBlocked, "Quarantined"):
            services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)


class ScanTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self)

    def test_scan_both_codes_starts_operation(self):
        op = services.scan_and_start(
            op_at(self.traveler, 10), self.operator, self.machine.qr_token, self.traveler.barcode, NOW)
        self.assertEqual(op.status, OP.IN_PROGRESS)

    def test_forged_machine_code_rejected(self):
        with self.assertRaises(InvalidQRToken):
            services.scan_and_start(op_at(self.traveler, 10), self.operator, "MACHINE:1", self.traveler.barcode, NOW)

    def test_wrong_kind_of_code_rejected(self):
        with self.assertRaises(InvalidQRToken):
            services.scan_and_start(
                op_at(self.traveler, 10), self.operator, make_token(QRKind.SHIPMENT, 1), self.traveler.barcode, NOW)

    def test_barcode_of_another_job_rejected(self):
        from .helpers import make_ready_part, make_work_order
        part2 = make_ready_part(self.engineer, self.manager, number="P-201")
        t2 = services.release_work_order(make_work_order(self.lead, part2, number="WO-2"), self.lead)
        with self.assertRaisesMessage(OperationBlocked, "different job"):
            services.scan_and_start(
                op_at(self.traveler, 10), self.operator, self.machine.qr_token, t2.barcode, NOW)
        self.assertEqual(op_at(self.traveler, 10).status, OP.PENDING)


class FirstPieceTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self)
        self.op = services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)

    def test_starts_in_setup_mode(self):
        self.assertEqual(self.op.run.state, ProductionRun.State.SETUP)

    def test_counter_locked_until_qa_signs_off(self):
        with self.assertRaises(FirstPieceLocked):
            services.record_parts(self.op, self.operator, 1, NOW)
        self.op.run.refresh_from_db()
        self.assertEqual(self.op.run.parts_produced, 0)

    def test_cannot_complete_without_first_piece(self):
        with self.assertRaises(FirstPieceLocked):
            services.complete_operation(self.op, self.operator, NOW)

    def test_qa_signoff_unlocks_counter(self):
        sign_off(self.op.run, self.qa, NOW)
        run = services.record_parts(self.op, self.operator, 2, NOW)
        self.assertEqual(run.parts_produced, 2)
        self.assertEqual(run.first_piece_cleared_by, self.qa)

    def test_only_qa_with_permission_can_sign_off(self):
        for user in (self.operator, self.operator2, self.lead, self.engineer):
            with self.assertRaises(NotAuthorized):
                sign_off(self.op.run, user, NOW)

    def test_operator_cannot_clear_own_first_piece(self):
        # Even if an operator were ever granted the permission, same-person sign-off is refused.
        self.op.operator = self.qa
        self.op.save()
        with self.assertRaisesMessage(OperationBlocked, "own first piece"):
            sign_off(ProductionRun.objects.get(pk=self.op.run.pk), self.qa, NOW)

    def test_cannot_sign_off_twice(self):
        sign_off(self.op.run, self.qa, NOW)
        with self.assertRaisesMessage(OperationBlocked, "already signed off"):
            sign_off(self.op.run, self.qa2, NOW)

    def test_signoff_is_audited(self):
        sign_off(self.op.run, self.qa, NOW)
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.Action.SIGN_OFF, user=self.qa, remarks="First-piece sign-off").exists())

    def test_step_without_first_piece_requirement_starts_running(self):
        sign_off(self.op.run, self.qa, NOW)
        services.record_parts(self.op, self.operator, 5, NOW)
        services.complete_operation(self.op, self.operator, NOW + timedelta(minutes=30))
        later = NOW + timedelta(hours=3)
        machine2 = make_machine_ready("CNC-06", later, self.operator)
        op20 = services.start_operation(op_at(self.traveler, 20), self.operator, machine2, later)
        self.assertEqual(op20.run.state, ProductionRun.State.RUNNING)


class CounterAndCompleteTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self, quantity=5)
        self.op = services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        sign_off(self.op.run, self.qa, NOW)

    def test_cannot_exceed_lot(self):
        services.record_parts(self.op, self.operator, 4, NOW)
        with self.assertRaisesMessage(OperationBlocked, "exceed"):
            services.record_parts(self.op, self.operator, 2, NOW)

    def test_rejects_zero_negative_and_non_integers(self):
        for bad in (0, -1, 1.5, "3", True, None):
            with self.assertRaises(OperationBlocked):
                services.record_parts(self.op, self.operator, bad, NOW)

    def test_cannot_complete_short_of_lot(self):
        services.record_parts(self.op, self.operator, 4, NOW)
        with self.assertRaisesMessage(OperationBlocked, "4 of 5"):
            services.complete_operation(self.op, self.operator, NOW)

    def test_complete_records_cycle_time_and_signoff(self):
        services.record_parts(self.op, self.operator, 5, NOW)
        done = services.complete_operation(self.op, self.operator, NOW + timedelta(minutes=45))
        self.assertEqual(done.status, OP.COMPLETED)
        self.assertEqual(done.cycle_seconds, 45 * 60)
        self.assertEqual(done.signed_off_by, self.operator)
        self.assertEqual(done.clock_out, NOW + timedelta(minutes=45))

    def test_cannot_complete_twice(self):
        services.record_parts(self.op, self.operator, 5, NOW)
        services.complete_operation(self.op, self.operator, NOW)
        with self.assertRaises(OperationBlocked):
            services.complete_operation(self.op, self.operator, NOW)

    def test_cannot_record_on_pending_or_completed_step(self):
        with self.assertRaises(OperationBlocked):
            services.record_parts(op_at(self.traveler, 20), self.operator, 1, NOW)

    def test_only_operators_count_and_complete(self):
        with self.assertRaises(NotAuthorized):
            services.record_parts(self.op, self.qa, 1, NOW)
        services.record_parts(self.op, self.operator, 5, NOW)
        with self.assertRaises(NotAuthorized):
            services.complete_operation(self.op, self.qa, NOW)


class HoldTimerTests(ProductionTestCase):
    """PRD 8: Op 20 carries a 90 minute mandatory hold before Op 30 may start."""

    def setUp(self):
        self.traveler, self.machine = released_traveler(self, quantity=2)
        run_through(self, self.traveler, self.machine, 10, NOW, 2)
        self.t20 = NOW + timedelta(hours=1)
        self.m2 = make_machine_ready("CNC-06", self.t20, self.operator)
        run_through(self, self.traveler, self.m2, 20, self.t20, 2)
        self.done20 = self.t20 + timedelta(minutes=30)  # signed off here; hold starts now
        self.m3 = make_machine_ready("CNC-07", self.done20, self.operator)

    def test_hold_starts_at_signoff(self):
        op = op_at(self.traveler, 20)
        self.assertEqual(op.hold_until, self.done20 + timedelta(minutes=90))

    def test_next_step_blocked_with_remaining_time(self):
        with self.assertRaises(HoldActive) as ctx:
            services.start_operation(op_at(self.traveler, 30), self.operator, self.m3, self.done20 + timedelta(minutes=8))
        self.assertIn("Mandatory Cooling Hold Active (1 hr 22 min remaining)", str(ctx.exception))
        self.assertEqual(op_at(self.traveler, 30).status, OP.PENDING)

    def test_blocked_one_minute_before_end(self):
        t = self.done20 + timedelta(minutes=89)
        make_machine_ready("CNC-07b", t, self.operator)
        with self.assertRaises(HoldActive):
            services.start_operation(op_at(self.traveler, 30), self.operator, self.m3, t)

    def test_allowed_exactly_when_hold_ends(self):
        t = self.done20 + timedelta(minutes=90)
        m = make_machine_ready("CNC-08", t, self.operator)
        op = services.start_operation(op_at(self.traveler, 30), self.operator, m, t)
        self.assertEqual(op.status, OP.IN_PROGRESS)

    def test_remaining_time_rounds_up(self):
        t = self.done20 + timedelta(minutes=89, seconds=30)
        with self.assertRaisesMessage(HoldActive, "1 min remaining"):
            services.start_operation(op_at(self.traveler, 30), self.operator, self.m3, t)

    def test_blocking_hold_helper(self):
        op, remaining = services.blocking_hold(self.traveler, self.done20 + timedelta(minutes=30))
        self.assertEqual(op.routing_operation.op_number, 20)
        self.assertEqual(remaining, timedelta(minutes=60))
        self.assertIsNone(services.blocking_hold(self.traveler, self.done20 + timedelta(minutes=91)))

    def test_steps_without_hold_do_not_block(self):
        self.assertIsNone(op_at(self.traveler, 10).hold_until)

    def test_format_remaining(self):
        f = services.format_remaining
        self.assertEqual(f(timedelta(minutes=82)), "1 hr 22 min")
        self.assertEqual(f(timedelta(minutes=5)), "5 min")
        self.assertEqual(f(timedelta(seconds=1)), "1 min")
        self.assertEqual(f(timedelta(minutes=60)), "1 hr 0 min")
        self.assertEqual(f(timedelta(0)), "0 min")


class CompletionAndTraceTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self, quantity=2, op20_hold=0)
        t = NOW
        for number, code in ((10, "CNC-04"), (20, "CNC-06"), (30, "CNC-07")):
            m = self.machine if number == 10 else make_machine_ready(code, t, self.operator)
            run_through(self, self.traveler, m, number, t, 2)
            t += timedelta(hours=1)

    def test_traveler_not_complete_until_final_pdi_done(self):
        self.traveler.refresh_from_db()
        self.assertEqual(self.traveler.status, JobTraveler.Status.ACTIVE)
        self.assertEqual(services.next_pending_operation(self.traveler).routing_operation.op_number, 40)

    def test_serials_refused_before_completion(self):
        with self.assertRaises(OperationBlocked):
            services.generate_serials(self.traveler, self.lead)

    def _finish(self):
        # Step 6 will do this via supervisor approval; tests stand in for that hook.
        TravelerOperation.objects.filter(pk=op_at(self.traveler, 40).pk).update(status=OP.COMPLETED)
        self.traveler.status = JobTraveler.Status.COMPLETED
        self.traveler.save(update_fields=["status"])

    def test_serials_issued_once_and_unique(self):
        self._finish()
        serials = services.generate_serials(self.traveler, self.lead)
        self.assertEqual([s.serial_number for s in serials], ["WO-1-0001", "WO-1-0002"])
        with self.assertRaises(OperationBlocked):
            services.generate_serials(self.traveler, self.lead)
        self.assertEqual(PartSerial.objects.count(), 2)

    def test_forward_and_backward_trace(self):
        self._finish()
        services.generate_serials(self.traveler, self.lead)
        wos = services.forward_trace(" h-7788 ")
        self.assertEqual([w.number for w in wos], ["WO-1"])
        self.assertEqual(services.forward_trace("NOPE"), [])
        birth = services.backward_trace("wo-1-0002")
        self.assertEqual(birth["work_order"].heat_number, "H-7788")
        ops = {o.routing_operation.op_number: o for o in birth["operations"]}
        self.assertEqual(ops[10].operator, self.operator)
        self.assertEqual(ops[10].machine, self.machine)
        self.assertIsNone(services.backward_trace("missing"))
