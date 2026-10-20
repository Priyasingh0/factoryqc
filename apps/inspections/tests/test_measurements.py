from datetime import timedelta
from decimal import Decimal

from apps.core.models import AuditLog
from apps.inspections import services
from apps.inspections.exceptions import (
    InspectionFrozen, InspectionLocked, InvalidMeasurement, NotAuthorized,
)
from apps.inspections.models import InspectionStatus, Measurement
from apps.plant.exceptions import GaugeNotUsable
from apps.plant.models import Gauge

from .helpers import (
    EXPIRED_ON, NOW, InspectionTestCase, begin_first_piece, enter, field_named, lot_in_setup, make_gauge,
)

D = Decimal


class MeasurementTests(InspectionTestCase):
    def setUp(self):
        self.traveler, self.machine = lot_in_setup(self)
        self.insp = begin_first_piece(self, self.traveler, self.machine)

    # --- tolerance (PRD 4.2, 5.3): Shaft diameter is 25.0000 +0.05 / -0.02 -----

    def test_limits_are_inclusive(self):
        self.assertTrue(enter(self, self.insp, "Shaft diameter", "25.0500").is_within_tolerance)

    def test_just_over_upper_limit_fails(self):
        self.assertFalse(enter(self, self.insp, "Shaft diameter", "25.0501").is_within_tolerance)

    def test_lower_limit_inclusive(self):
        self.assertTrue(enter(self, self.insp, "Shaft diameter", "24.9800").is_within_tolerance)

    def test_just_under_lower_limit_fails(self):
        self.assertFalse(enter(self, self.insp, "Shaft diameter", "24.9799").is_within_tolerance)

    def test_nominal_passes_and_stores_exact_decimal(self):
        m = enter(self, self.insp, "Shaft diameter", "25")
        m.refresh_from_db()
        self.assertEqual(m.value_numeric, D("25.0000"))
        self.assertIsInstance(m.value_numeric, D)

    def test_visual_pass_and_fail(self):
        self.assertTrue(enter(self, self.insp, "Burrs removed", True, gauge="").is_within_tolerance)

    def test_visual_fail_is_out_of_tolerance(self):
        self.assertFalse(enter(self, self.insp, "Burrs removed", False, gauge="").is_within_tolerance)

    def test_verdict_is_computed_not_trusted(self):
        field = field_named(self.insp.template, "Shaft diameter")
        m = Measurement(inspection=self.insp, field=field, value_numeric=D("30"), gauge=self.mic,
                        entered_by=self.qa, is_within_tolerance=True)
        m.save()
        self.assertFalse(m.is_within_tolerance)

    # --- input validation ----------------------------------------------------

    def test_rejects_non_numeric_and_non_finite(self):
        for bad in ("abc", "", "NaN", "Infinity", "-Infinity", None, True, 25.0, "1e400000"):
            with self.assertRaises(InvalidMeasurement, msg=repr(bad)):
                enter(self, self.insp, "Shaft diameter", bad)
        self.assertFalse(Measurement.objects.exists())

    def test_rejects_more_than_four_decimals(self):
        with self.assertRaisesMessage(InvalidMeasurement, "4 decimal"):
            enter(self, self.insp, "Shaft diameter", "25.00004")

    def test_rejects_absurdly_large_value(self):
        with self.assertRaises(InvalidMeasurement):
            enter(self, self.insp, "Shaft diameter", "1000000000")

    def test_visual_needs_a_real_bool(self):
        for bad in ("PASS", 1, None, "true"):
            with self.assertRaises(InvalidMeasurement):
                enter(self, self.insp, "Burrs removed", bad, gauge="")

    def test_accepts_int_and_decimal_input(self):
        self.assertTrue(enter(self, self.insp, "Shaft diameter", 25).is_within_tolerance)

    def test_one_reading_per_field_and_no_overwrite(self):
        enter(self, self.insp, "Shaft diameter", "99")
        with self.assertRaisesMessage(InvalidMeasurement, "already recorded"):
            enter(self, self.insp, "Shaft diameter", "25")
        self.assertEqual(Measurement.objects.get().value_numeric, D("99.0000"))

    def test_recorded_reading_cannot_be_edited_or_deleted_even_while_open(self):
        m = enter(self, self.insp, "Shaft diameter", "26")
        m.value_numeric = D("25")
        with self.assertRaises(InspectionLocked):
            m.save()
        with self.assertRaises(InspectionLocked):
            m.delete()
        self.assertFalse(Measurement.objects.get().is_within_tolerance)

    def test_field_from_another_template_refused(self):
        from apps.checklists.models import ChecklistTemplate, TemplateField
        other = ChecklistTemplate.objects.filter(checklist_type="PDI").first()
        foreign = other.fields.first()
        with self.assertRaisesMessage(InvalidMeasurement, "not on this inspection"):
            services.record_measurement(self.insp, foreign, value="25", gauge_id="MIC-1", user=self.qa, now=NOW)

    def test_fields_may_be_entered_in_any_order(self):
        enter(self, self.insp, "Burrs removed", True, gauge="")
        enter(self, self.insp, "Shaft diameter", "25")
        self.assertEqual(Measurement.objects.count(), 2)

    # --- who may enter -------------------------------------------------------

    def test_only_the_opening_inspector_may_enter(self):
        with self.assertRaisesMessage(NotAuthorized, "opened this inspection"):
            enter(self, self.insp, "Shaft diameter", "25", user=self.qa2)

    def test_other_roles_cannot_enter(self):
        for user in (self.operator, self.supervisor, self.qa_head, self.lead):
            with self.assertRaises(NotAuthorized):
                enter(self, self.insp, "Shaft diameter", "25", user=user)

    def test_submitted_inspection_rejects_entries(self):
        enter(self, self.insp, "Shaft diameter", "25")
        enter(self, self.insp, "Burrs removed", True, gauge="")
        services.submit_inspection(self.insp, self.qa, now=NOW)
        with self.assertRaises(InspectionLocked):
            enter(self, self.insp, "Shaft diameter", "25")

    # --- audit (PRD 13.3) ----------------------------------------------------

    def test_every_reading_is_audited_with_gauge_and_verdict(self):
        m = enter(self, self.insp, "Shaft diameter", "26.0000")
        row = AuditLog.objects.get(model_name="inspections.Measurement", object_id=str(m.pk))
        self.assertEqual(row.user, self.qa)
        self.assertEqual(row.action, AuditLog.Action.CREATE)
        self.assertIn("26.0000", row.new_value)
        self.assertIn("OUT OF TOLERANCE", row.remarks)
        self.assertIn("MIC-1", row.remarks)


class GaugeRuleTests(InspectionTestCase):
    """PRD 2.3: the exact gauge is logged, checked against the calibration register."""

    def setUp(self):
        self.traveler, self.machine = lot_in_setup(self)
        self.insp = begin_first_piece(self, self.traveler, self.machine)

    def test_gauge_is_mandatory_for_measured_fields(self):
        for blank in ("", "   ", None):
            with self.assertRaisesMessage(InvalidMeasurement, "gauge serial"):
                enter(self, self.insp, "Shaft diameter", "25", gauge=blank)

    def test_unknown_gauge_blocked(self):
        with self.assertRaisesMessage(GaugeNotUsable, "Unknown gauge"):
            enter(self, self.insp, "Shaft diameter", "25", gauge="NOPE-1")

    def test_expired_gauge_blocks_the_entry(self):
        make_gauge("MIC-OLD", "MICROMETER", expiry=EXPIRED_ON)
        with self.assertRaisesMessage(GaugeNotUsable, "Expired Tool Calibration"):
            enter(self, self.insp, "Shaft diameter", "25", gauge="MIC-OLD")
        self.assertFalse(Measurement.objects.exists())

    def test_gauge_expiring_today_is_still_valid(self):
        make_gauge("MIC-TODAY", "MICROMETER", expiry=NOW.date())
        self.assertTrue(enter(self, self.insp, "Shaft diameter", "25", gauge="MIC-TODAY").is_within_tolerance)

    def test_gauge_that_expired_yesterday_is_blocked(self):
        make_gauge("MIC-Y", "MICROMETER", expiry=NOW.date() - timedelta(days=1))
        with self.assertRaises(GaugeNotUsable):
            enter(self, self.insp, "Shaft diameter", "25", gauge="MIC-Y")

    def test_expiry_is_judged_at_the_time_of_the_reading(self):
        later = NOW + timedelta(days=400)
        with self.assertRaisesMessage(GaugeNotUsable, "Expired"):
            enter(self, self.insp, "Shaft diameter", "25", now=later)

    def test_inactive_gauge_blocked(self):
        make_gauge("MIC-OFF", "MICROMETER", active=False)
        with self.assertRaisesMessage(GaugeNotUsable, "inactive"):
            enter(self, self.insp, "Shaft diameter", "25", gauge="MIC-OFF")

    def test_wrong_kind_of_gauge_blocked(self):
        with self.assertRaisesMessage(InvalidMeasurement, "Micrometer"):
            enter(self, self.insp, "Shaft diameter", "25", gauge="VC-104")

    def test_gauge_serial_is_trimmed(self):
        self.assertEqual(enter(self, self.insp, "Shaft diameter", "25", gauge="  MIC-1 ").gauge, self.mic)

    def test_blocked_entry_is_recorded_in_the_audit_log(self):
        make_gauge("MIC-OLD", "MICROMETER", expiry=EXPIRED_ON)
        with self.assertRaises(GaugeNotUsable):
            enter(self, self.insp, "Shaft diameter", "25", gauge="MIC-OLD")
        self.assertTrue(AuditLog.objects.filter(
            user=self.qa, remarks__contains="Reading blocked: Expired Tool Calibration").exists())

    def test_visual_checks_do_not_need_a_gauge(self):
        m = enter(self, self.insp, "Burrs removed", True, gauge="")
        self.assertIsNone(m.gauge)
