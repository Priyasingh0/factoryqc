from datetime import timedelta

from django.db.models import QuerySet
from django.urls import reverse

from apps.accounts.tests.helpers import TEST_PASSWORD
from apps.handover import monitor
from apps.plant.tests.helpers import make_machine
from apps.production import services as production
from apps.production.models import JobTraveler, ProductionRun, TravelerOperation
from apps.production.tests.helpers import NOW, op_at, released_traveler, run_through, sign_off

from .base import HandoverTestCase

HOUR = timedelta(hours=1)
MIN = timedelta(minutes=1)


class MonitorBase(HandoverTestCase):
    def lot(self, quantity=5, **routing):
        traveler, machine = released_traveler(self, quantity=quantity, **routing)
        QuerySet.update(JobTraveler.objects.filter(pk=traveler.pk), created_at=NOW)
        traveler.refresh_from_db()
        return traveler, machine

    def samples(self, traveler, number=10, seconds=(600, 600, 600)):
        step = op_at(traveler, number).routing_operation
        for s in seconds:
            other = JobTraveler.objects.create(work_order=traveler.work_order, quantity=1, is_rework=True)
            TravelerOperation.objects.create(
                traveler=other, routing_operation=step, status=TravelerOperation.Status.COMPLETED, cycle_seconds=s)


class JobTimingTests(MonitorBase):
    def test_a_lot_not_yet_started_is_waiting_and_all_its_time_is_queue_time(self):
        traveler, _ = self.lot()
        t = monitor.job_timing(traveler, NOW + HOUR)
        self.assertEqual((t.state, t.elapsed, t.active, t.hold, t.queue), ("WAITING", HOUR, timedelta(0), timedelta(0), HOUR))
        self.assertEqual((t.waiting, t.waiting_since, t.stuck), (HOUR, NOW, False))
        self.assertEqual(t.current_operation.routing_operation.op_number, 10)

    def test_stuck_means_strictly_longer_than_the_queue_limit(self):
        traveler, _ = self.lot()
        self.assertFalse(monitor.job_timing(traveler, NOW + monitor.QUEUE_LIMIT).stuck)
        self.assertTrue(monitor.job_timing(traveler, NOW + monitor.QUEUE_LIMIT + MIN).stuck)

    def test_a_running_lot_counts_active_time_and_the_rest_is_queue(self):
        traveler, machine = self.lot()
        op = op_at(traveler, 10)
        production.start_operation(op, self.operator, machine, NOW + HOUR)
        t = monitor.job_timing(traveler, NOW + 2 * HOUR)
        self.assertEqual((t.state, t.active, t.queue, t.elapsed), ("RUNNING", HOUR, HOUR, 2 * HOUR))
        self.assertEqual(t.current_operation.pk, op.pk)
        self.assertFalse(t.stuck)

    def test_paused_time_is_not_active_time(self):
        traveler, machine = self.lot()
        op = op_at(traveler, 10)
        production.start_operation(op, self.operator, machine, NOW)
        QuerySet.update(ProductionRun.objects.filter(traveler_operation=op), paused_seconds=600)
        t = monitor.job_timing(traveler, NOW + HOUR)
        self.assertEqual(t.active, HOUR - 10 * MIN)

    def test_hold_time_is_counted_as_hold_not_as_queue(self):
        traveler, machine = self.lot()
        run_through(self, traveler, machine, 10, NOW, 5)                 # ends 30 min later, no hold
        run_through(self, traveler, machine, 20, NOW + HOUR, 5)          # 11:00-11:30, then a 90 minute hold
        during = monitor.job_timing(traveler, NOW + 2 * HOUR)
        self.assertEqual(during.state, "HOLD")
        self.assertEqual((during.waiting, during.stuck), (timedelta(0), False))
        self.assertEqual(during.hold, 30 * MIN)
        self.assertEqual(during.active, HOUR)
        self.assertEqual(during.queue, 30 * MIN)                          # the gap between op 10 and op 20
        after = monitor.job_timing(traveler, NOW + 3 * HOUR + 30 * MIN)
        self.assertEqual((after.state, after.hold, after.waiting), ("WAITING", 90 * MIN, 30 * MIN))
        self.assertEqual(after.waiting_since, NOW + 3 * HOUR)             # the hold ended

    def test_quarantined_and_finished_lots_are_not_waiting(self):
        traveler, machine = self.lot()
        run_through(self, traveler, machine, 10, NOW, 5)
        QuerySet.update(JobTraveler.objects.filter(pk=traveler.pk), status=JobTraveler.Status.QUARANTINED)
        traveler.refresh_from_db()
        t = monitor.job_timing(traveler, NOW + 40 * HOUR)
        self.assertEqual((t.state, t.stuck), ("BLOCKED", False))
        QuerySet.update(JobTraveler.objects.filter(pk=traveler.pk), status=JobTraveler.Status.COMPLETED)
        traveler.refresh_from_db()
        t = monitor.job_timing(traveler, NOW + 40 * HOUR)
        self.assertEqual((t.state, t.stuck), ("DONE", False))
        self.assertEqual(t.elapsed, 30 * MIN)                              # stops at the last sign-off


class MachineTileTests(MonitorBase):
    def test_usual_cycle_needs_enough_completed_runs(self):
        traveler, _ = self.lot()
        step = op_at(traveler, 10).routing_operation
        self.samples(traveler, seconds=(600, 600))
        self.assertIsNone(monitor.usual_cycle(step))
        self.samples(traveler, seconds=(900,))
        self.assertEqual(monitor.usual_cycle(step), timedelta(seconds=700))

    def test_idle_and_inactive_machines(self):
        make_machine("CNC-OFF", active=False)
        make_machine("CNC-IDLE")
        tiles = {t.machine.code: t for t in monitor.machine_tiles(NOW)}
        self.assertNotIn("CNC-OFF", tiles)
        self.assertEqual(tiles["CNC-IDLE"].status, "IDLE")

    def test_running_normally_then_slow_only_beyond_one_and_a_half_times_usual(self):
        traveler, machine = self.lot()
        self.samples(traveler)                                              # usual cycle: 10 minutes
        op = op_at(traveler, 10)
        production.start_operation(op, self.operator, machine, NOW)
        at_limit = {t.machine.pk: t for t in monitor.machine_tiles(NOW + 15 * MIN)}[machine.pk]
        self.assertEqual((at_limit.status, at_limit.running, at_limit.usual), ("OK", 15 * MIN, 10 * MIN))
        over = {t.machine.pk: t for t in monitor.machine_tiles(NOW + 16 * MIN)}[machine.pk]
        self.assertEqual(over.status, "SLOW")
        self.assertEqual(over.operation.pk, op.pk)

    def test_without_history_a_machine_is_never_called_slow(self):
        traveler, machine = self.lot()
        production.start_operation(op_at(traveler, 10), self.operator, machine, NOW)
        tile = {t.machine.pk: t for t in monitor.machine_tiles(NOW + 40 * HOUR)}[machine.pk]
        self.assertEqual(tile.status, "OK")

    def test_a_quarantined_lot_no_longer_occupies_its_machine(self):
        traveler, machine = self.lot()
        production.start_operation(op_at(traveler, 10), self.operator, machine, NOW)
        QuerySet.update(JobTraveler.objects.filter(pk=traveler.pk), status=JobTraveler.Status.QUARANTINED)
        tile = {t.machine.pk: t for t in monitor.machine_tiles(NOW + HOUR)}[machine.pk]
        self.assertEqual(tile.status, "IDLE")


class FloorReportTests(MonitorBase):
    def test_stuck_lots_are_listed_longest_waiting_first(self):
        traveler, _ = self.lot()
        report = monitor.floor_report(NOW + 6 * HOUR)
        self.assertEqual([j.traveler.pk for j in report["stuck"]], [traveler.pk])
        self.assertEqual(report["lots"][0].waiting, 6 * HOUR)
        self.assertEqual(monitor.floor_report(NOW + HOUR)["stuck"], [])

    def test_only_active_lots_are_monitored(self):
        traveler, _ = self.lot()
        QuerySet.update(JobTraveler.objects.filter(pk=traveler.pk), status=JobTraveler.Status.CLOSED)
        self.assertEqual(monitor.floor_report(NOW + 9 * HOUR)["lots"], [])


class MonitorViewTests(MonitorBase):
    def login(self, user):
        self.client.login(username=user.username, password=TEST_PASSWORD)

    def test_supervisor_sees_stuck_lots_and_machine_tiles(self):
        traveler, machine = self.lot()
        self.login(self.sup)
        from unittest import mock
        from django.utils import timezone
        with mock.patch.object(timezone, "now", return_value=NOW + 6 * HOUR):
            page = self.client.get(reverse("handover:flow_monitor"))
        self.assertContains(page, "Stuck in queue")
        self.assertContains(page, traveler.work_order.number)
        self.assertContains(page, machine.code)

    def test_access_follows_permission(self):
        self.assertEqual(self.client.get(reverse("handover:flow_monitor")).status_code, 302)
        self.login(self.warehouse)
        self.assertEqual(self.client.get(reverse("handover:flow_monitor")).status_code, 403)
