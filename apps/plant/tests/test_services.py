from datetime import date, timedelta

from django.core import mail
from django.test import TestCase

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog, Notification
from apps.core.signing import QRKind, make_token
from apps.plant import services
from apps.plant.exceptions import GaugeNotUsable, MachineNotReady, PMCheckError
from apps.plant.models import MachinePMCheck, Shift

from .helpers import at, make_gauge, make_machine


class ShiftForTests(TestCase):
    def test_shift_boundaries(self):
        cases = [
            (at(2026, 10, 14, 5, 59), (date(2026, 10, 13), Shift.NIGHT)),
            (at(2026, 10, 14, 0, 30), (date(2026, 10, 13), Shift.NIGHT)),
            (at(2026, 10, 14, 6, 0), (date(2026, 10, 14), Shift.MORNING)),
            (at(2026, 10, 14, 13, 59), (date(2026, 10, 14), Shift.MORNING)),
            (at(2026, 10, 14, 14, 0), (date(2026, 10, 14), Shift.EVENING)),
            (at(2026, 10, 14, 21, 59), (date(2026, 10, 14), Shift.EVENING)),
            (at(2026, 10, 14, 22, 0), (date(2026, 10, 14), Shift.NIGHT)),
            (at(2026, 10, 14, 23, 59), (date(2026, 10, 14), Shift.NIGHT)),
        ]
        for moment, expected in cases:
            self.assertEqual(services.shift_for(moment), expected, moment)


class MachineReadinessTests(TestCase):
    def setUp(self):
        self.machine = make_machine()
        self.operator = make_user("op", Role.MACHINE_OPERATOR)
        self.now = at(2026, 10, 14, 10)  # Morning shift, 14 Oct

    def _check(self, **kwargs):
        defaults = dict(
            machine=self.machine, date=date(2026, 10, 14), shift=Shift.MORNING,
            performed_by=self.operator, completed=True, safety_zero_point_ok=True,
        )
        defaults.update(kwargs)
        return MachinePMCheck.objects.create(**defaults)

    def test_locked_without_any_check(self):
        result = services.machine_readiness(self.machine, self.now)
        self.assertFalse(result.ready)
        self.assertIn("not recorded", result.reason)

    def test_ready_with_completed_and_safe_check(self):
        self._check()
        self.assertTrue(services.machine_readiness(self.machine, self.now).ready)

    def test_locked_when_checklist_incomplete(self):
        self._check(completed=False)
        result = services.machine_readiness(self.machine, self.now)
        self.assertFalse(result.ready)
        self.assertIn("not complete", result.reason)

    def test_locked_when_safety_zero_point_failed(self):
        self._check(safety_zero_point_ok=False)
        result = services.machine_readiness(self.machine, self.now)
        self.assertFalse(result.ready)
        self.assertIn("Safety zero-point", result.reason)

    def test_previous_shift_check_does_not_count(self):
        self._check(shift=Shift.NIGHT, date=date(2026, 10, 13))
        self.assertFalse(services.machine_readiness(self.machine, self.now).ready)

    def test_yesterdays_morning_check_does_not_count(self):
        self._check(date=date(2026, 10, 13))
        self.assertFalse(services.machine_readiness(self.machine, self.now).ready)

    def test_check_expires_when_shift_changes(self):
        self._check()
        evening = at(2026, 10, 14, 14, 0)
        self.assertFalse(services.machine_readiness(self.machine, evening).ready)

    def test_inactive_machine_is_never_ready(self):
        self.machine.is_active = False
        self.machine.save()
        self._check()
        result = services.machine_readiness(self.machine, self.now)
        self.assertFalse(result.ready)
        self.assertIn("inactive", result.reason)

    def test_bulk_readiness_matches_single(self):
        other = make_machine("VMC-02")
        self._check()
        rows = dict(
            (m.code, r.ready)
            for m, r in services.readiness_for_machines([self.machine, other], self.now)
        )
        self.assertEqual(rows, {"CNC-04": True, "VMC-02": False})


class AssertMachineReadyAlertTests(TestCase):
    def setUp(self):
        self.machine = make_machine()
        self.now = at(2026, 10, 14, 10)
        self.maint = make_user("maint", Role.MAINTENANCE_SUPERVISOR, email="maint@factory.local")

    def test_blocks_and_alerts_maintenance_supervisor(self):
        with self.assertRaises(MachineNotReady):
            services.assert_machine_ready(self.machine, self.now)
        note = Notification.objects.get()
        self.assertEqual(note.recipient, self.maint)
        self.assertEqual(note.status, Notification.Status.SENT)
        self.assertIn("CNC-04", note.subject)
        self.assertEqual(note.related_object_id, str(self.machine.pk))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["maint@factory.local"])

    def test_repeat_attempts_in_same_shift_do_not_spam(self):
        for _ in range(3):
            with self.assertRaises(MachineNotReady):
                services.assert_machine_ready(self.machine, self.now)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_new_shift_alerts_again(self):
        with self.assertRaises(MachineNotReady):
            services.assert_machine_ready(self.machine, self.now)
        with self.assertRaises(MachineNotReady):
            services.assert_machine_ready(self.machine, at(2026, 10, 14, 15))
        self.assertEqual(Notification.objects.count(), 2)

    def test_ready_machine_passes_silently(self):
        operator = make_user("op", Role.MACHINE_OPERATOR)
        MachinePMCheck.objects.create(
            machine=self.machine, date=date(2026, 10, 14), shift=Shift.MORNING,
            performed_by=operator, completed=True, safety_zero_point_ok=True,
        )
        services.assert_machine_ready(self.machine, self.now)
        self.assertEqual(Notification.objects.count(), 0)

    def test_inactive_machine_is_blocked_without_pm_alert(self):
        self.machine.is_active = False
        self.machine.save()
        with self.assertRaises(MachineNotReady):
            services.assert_machine_ready(self.machine, self.now)
        self.assertEqual(Notification.objects.count(), 0)

    def test_alert_is_kept_on_record_when_nobody_holds_the_role(self):
        self.maint.is_active = False
        self.maint.save()
        with self.assertRaises(MachineNotReady):
            services.assert_machine_ready(self.machine, self.now)
        note = Notification.objects.get()
        self.assertIsNone(note.recipient)
        self.assertEqual(note.status, Notification.Status.PENDING)
        self.assertEqual(note.recipient_role, Role.MAINTENANCE_SUPERVISOR)


class RecordPMCheckTests(TestCase):
    def setUp(self):
        self.machine = make_machine()
        self.user = make_user("op", Role.MACHINE_OPERATOR)
        self.now = at(2026, 10, 14, 10)

    def _record(self, **kwargs):
        defaults = dict(completed=True, safety_zero_point_ok=True, now=self.now)
        defaults.update(kwargs)
        return services.record_pm_check(self.machine, self.user, **defaults)

    def test_creates_check_for_current_shift_and_audits(self):
        check = self._record(remarks="all good")
        self.assertEqual((check.date, check.shift), (date(2026, 10, 14), Shift.MORNING))
        self.assertTrue(services.machine_readiness(self.machine, self.now).ready)
        entry = AuditLog.objects.get(action=AuditLog.Action.CREATE, model_name="plant.MachinePMCheck")
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.remarks, "all good")

    def test_failed_check_can_be_corrected_in_same_shift(self):
        self._record(safety_zero_point_ok=False)
        self.assertFalse(services.machine_readiness(self.machine, self.now).ready)
        self._record()
        self.assertEqual(MachinePMCheck.objects.count(), 1)
        self.assertTrue(services.machine_readiness(self.machine, self.now).ready)
        update = AuditLog.objects.get(action=AuditLog.Action.UPDATE)
        self.assertIn("safety_zero_point_ok=False", update.previous_value)
        self.assertIn("safety_zero_point_ok=True", update.new_value)

    def test_passed_check_cannot_be_overwritten(self):
        self._record()
        with self.assertRaises(PMCheckError):
            self._record(completed=False)
        self.assertTrue(MachinePMCheck.objects.get().is_clear)

    def test_inactive_machine_is_refused(self):
        self.machine.is_active = False
        self.machine.save()
        with self.assertRaises(PMCheckError):
            self._record()
        self.assertEqual(MachinePMCheck.objects.count(), 0)


class GaugeCalibrationRuleTests(TestCase):
    def test_gauge_expiring_today_is_usable(self):
        services.assert_gauge_usable(make_gauge("VC-1", date.today()))

    def test_expired_gauge_is_blocked_with_prd_message(self):
        gauge = make_gauge("VC-2", date.today() - timedelta(days=1))
        with self.assertRaises(GaugeNotUsable) as ctx:
            services.assert_gauge_usable(gauge)
        message = str(ctx.exception)
        self.assertIn("Expired Tool Calibration", message)
        self.assertIn("VC-2", message)
        self.assertIn("tool crib", message)

    def test_inactive_gauge_is_blocked_even_if_in_date(self):
        with self.assertRaises(GaugeNotUsable):
            services.assert_gauge_usable(make_gauge("VC-3", active=False))

    def test_check_uses_the_supplied_date(self):
        gauge = make_gauge("VC-4", date(2026, 10, 14))
        services.assert_gauge_usable(gauge, on=date(2026, 10, 14))
        with self.assertRaises(GaugeNotUsable):
            services.assert_gauge_usable(gauge, on=date(2026, 10, 15))

    def test_get_usable_gauge_by_serial(self):
        gauge = make_gauge("VC-5")
        self.assertEqual(services.get_usable_gauge("  VC-5 "), gauge)

    def test_unknown_gauge_is_rejected(self):
        with self.assertRaises(GaugeNotUsable):
            services.get_usable_gauge("NOPE-1")

    def test_get_usable_gauge_rejects_expired(self):
        make_gauge("VC-6", date.today() - timedelta(days=5))
        with self.assertRaises(GaugeNotUsable):
            services.get_usable_gauge("VC-6")


class ResolveMachineQRTests(TestCase):
    def test_valid_token_returns_machine(self):
        machine = make_machine()
        self.assertEqual(services.resolve_machine_from_qr(machine.qr_token), machine)

    def test_tampered_token_is_rejected(self):
        token = make_machine().qr_token
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        with self.assertRaises(InvalidQRToken):
            services.resolve_machine_from_qr(bad)

    def test_token_for_deleted_machine_is_rejected(self):
        machine = make_machine()
        token = machine.qr_token
        machine.delete()
        with self.assertRaises(InvalidQRToken):
            services.resolve_machine_from_qr(token)

    def test_non_numeric_id_is_rejected(self):
        with self.assertRaises(InvalidQRToken):
            services.resolve_machine_from_qr(make_token(QRKind.MACHINE, "abc"))

    def test_wrong_kind_is_rejected(self):
        with self.assertRaises(InvalidQRToken):
            services.resolve_machine_from_qr(make_token(QRKind.TRAVELER, 1))
