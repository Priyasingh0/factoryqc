from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.core.exceptions import InvalidQRToken
from apps.core.signing import QRKind, read_token
from apps.plant.models import Gauge, GaugeType, MachinePMCheck, Part, Shift, ToolingProgram

from .helpers import make_gauge, make_machine


class GaugeModelTests(TestCase):
    def test_calibrated_when_expiry_is_today_or_later(self):
        today = date.today()
        self.assertTrue(make_gauge("G1", today).is_calibrated)
        self.assertTrue(make_gauge("G2", today + timedelta(days=1)).is_calibrated)

    def test_expired_when_expiry_was_yesterday(self):
        gauge = make_gauge("G3", date.today() - timedelta(days=1))
        self.assertFalse(gauge.is_calibrated)
        self.assertEqual(gauge.status, Gauge.Status.EXPIRED)

    def test_inactive_status_wins_over_valid_calibration(self):
        self.assertEqual(make_gauge("G4", active=False).status, Gauge.Status.INACTIVE)

    def test_valid_status(self):
        self.assertEqual(make_gauge("G5").status, Gauge.Status.VALID)

    def test_expiry_before_calibration_date_is_invalid(self):
        gauge = Gauge(
            gauge_id="G6", gauge_type=GaugeType.CMM,
            calibrated_on=date(2026, 5, 1), calibration_expiry=date(2026, 4, 1),
        )
        with self.assertRaises(ValidationError):
            gauge.full_clean()

    def test_gauge_id_is_unique(self):
        make_gauge("VC-104")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_gauge("VC-104")


class MachineModelTests(TestCase):
    def test_qr_token_round_trips_to_machine_id(self):
        machine = make_machine()
        self.assertEqual(read_token(machine.qr_token, QRKind.MACHINE), str(machine.pk))

    def test_unsaved_machine_has_no_token(self):
        from apps.plant.models import Machine

        self.assertEqual(Machine(code="X", name="x").qr_token, "")

    def test_machine_token_is_not_valid_as_traveler_token(self):
        machine = make_machine()
        with self.assertRaises(InvalidQRToken):
            read_token(machine.qr_token, QRKind.TRAVELER)

    def test_one_pm_check_per_machine_date_shift(self):
        machine = make_machine()
        user = make_user("op", Role.MACHINE_OPERATOR)
        kwargs = dict(machine=machine, date=date(2026, 10, 14), shift=Shift.MORNING, performed_by=user)
        MachinePMCheck.objects.create(**kwargs)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MachinePMCheck.objects.create(**kwargs)
        # A different shift is a different record.
        MachinePMCheck.objects.create(**{**kwargs, "shift": Shift.EVENING})

    def test_pm_check_is_clear_needs_both_flags(self):
        check = MachinePMCheck(completed=True, safety_zero_point_ok=False)
        self.assertFalse(check.is_clear)
        check.safety_zero_point_ok = True
        self.assertTrue(check.is_clear)


class PartModelTests(TestCase):
    def test_program_version_unique_per_part(self):
        part = Part.objects.create(part_number="P-100", name="Shaft")
        ToolingProgram.objects.create(part=part, program_name="OP20", version="1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ToolingProgram.objects.create(part=part, program_name="OP20", version="1")
        ToolingProgram.objects.create(part=part, program_name="OP20", version="2")
