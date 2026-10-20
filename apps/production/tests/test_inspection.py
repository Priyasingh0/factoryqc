from datetime import timedelta

from apps.production import services
from apps.production.exceptions import NotAuthorized, OperationBlocked
from apps.production.models import TravelerOperation

from .helpers import NOW, ProductionTestCase, op_at, released_traveler, sign_off

OP = TravelerOperation.Status


class IntervalInspectionTests(ProductionTestCase):
    """PRD 9.2: check every 60 min; 15 min grace, then the job timer pauses."""

    def setUp(self):
        self.traveler, self.machine = released_traveler(self, quantity=5, interval=60)
        self.op = services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        self.cleared = NOW + timedelta(minutes=10)
        sign_off(self.op.run, self.qa, self.cleared)
        self.op.run.refresh_from_db()

    def state(self, minutes):
        return services.inspection_state(self.op.run, self.cleared + timedelta(minutes=minutes))

    def test_clock_starts_at_first_piece_signoff(self):
        self.assertEqual(self.op.run.next_check_due_at, self.cleared + timedelta(minutes=60))

    def test_not_due_before_interval(self):
        self.assertFalse(self.state(59).due)

    def test_due_with_grace_countdown(self):
        s = self.state(60)
        self.assertTrue(s.due)
        self.assertFalse(s.overdue)
        self.assertIn("Periodic Quality Inspection Due (15 min left", s.message)

    def test_still_in_grace_at_15_minutes(self):
        self.assertFalse(self.state(75).overdue)

    def test_overdue_after_grace(self):
        s = self.state(76)
        self.assertTrue(s.overdue)
        self.assertIn("timer is paused", s.message)

    def test_complete_blocked_while_check_due(self):
        services.record_parts(self.op, self.operator, 5, self.cleared)
        with self.assertRaisesMessage(OperationBlocked, "Periodic Quality Inspection Due"):
            services.complete_operation(self.op, self.operator, self.cleared + timedelta(minutes=61))

    def test_logging_check_restarts_interval(self):
        at_check = self.cleared + timedelta(minutes=65)
        run = services.log_periodic_check(self.op.run, self.qa, at_check)
        self.assertEqual(run.next_check_due_at, at_check + timedelta(minutes=60))
        self.assertEqual(run.paused_seconds, 0)
        self.assertFalse(services.inspection_state(run, at_check + timedelta(minutes=10)).due)

    def test_overdue_time_is_excluded_from_cycle_time(self):
        # due at +60, grace ends +75, logged at +95 => 20 min paused
        services.log_periodic_check(self.op.run, self.qa, self.cleared + timedelta(minutes=95))
        services.record_parts(self.op, self.operator, 5, self.cleared + timedelta(minutes=96))
        done = services.complete_operation(self.op, self.operator, NOW + timedelta(minutes=110))
        self.op.run.refresh_from_db()
        self.assertEqual(self.op.run.paused_seconds, 20 * 60)
        self.assertEqual(done.cycle_seconds, 110 * 60 - 20 * 60)

    def test_only_qa_logs_checks(self):
        for user in (self.operator, self.lead, self.engineer):
            with self.assertRaises(NotAuthorized):
                services.log_periodic_check(self.op.run, user, self.cleared)


class PartsInspectionTests(ProductionTestCase):
    def setUp(self):
        self.traveler, self.machine = released_traveler(self, quantity=10, every_parts=4)
        self.op = services.start_operation(op_at(self.traveler, 10), self.operator, self.machine, NOW)
        sign_off(self.op.run, self.qa, NOW)

    def test_due_after_every_n_parts(self):
        run = services.record_parts(self.op, self.operator, 3, NOW)
        self.assertFalse(services.inspection_state(run, NOW).due)
        run = services.record_parts(self.op, self.operator, 1, NOW)
        self.assertTrue(services.inspection_state(run, NOW).due)

    def test_logging_resets_the_part_count(self):
        services.record_parts(self.op, self.operator, 4, NOW)
        run = services.log_periodic_check(self.op.run, self.qa, NOW)
        self.assertFalse(services.inspection_state(run, NOW).due)
        run = services.record_parts(self.op, self.operator, 3, NOW)
        self.assertFalse(services.inspection_state(run, NOW).due)
        run = services.record_parts(self.op, self.operator, 1, NOW)
        self.assertTrue(services.inspection_state(run, NOW).due)


class NoPeriodicInspectionTests(ProductionTestCase):
    def test_no_inspection_state_in_setup_mode(self):
        traveler, machine = released_traveler(self, quantity=5, interval=60)
        op = services.start_operation(op_at(traveler, 10), self.operator, machine, NOW)
        state = services.inspection_state(op.run, NOW + timedelta(hours=5))
        self.assertFalse(state.due)  # timer has not started: QA has not signed off yet

    def test_check_refused_when_step_has_none_configured(self):
        traveler, machine = released_traveler(self, quantity=1)
        op = services.start_operation(op_at(traveler, 10), self.operator, machine, NOW)
        sign_off(op.run, self.qa, NOW)
        with self.assertRaisesMessage(OperationBlocked, "no periodic inspection"):
            services.log_periodic_check(op.run, self.qa, NOW)

    def test_check_refused_during_setup(self):
        traveler, machine = released_traveler(self, quantity=1, interval=60)
        op = services.start_operation(op_at(traveler, 10), self.operator, machine, NOW)
        with self.assertRaises(OperationBlocked):
            services.log_periodic_check(op.run, self.qa, NOW)
