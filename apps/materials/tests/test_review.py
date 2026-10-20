from datetime import timedelta
from decimal import Decimal

from apps.core.models import AuditLog, Notification
from apps.materials import services
from apps.materials.exceptions import IntakeRejected, InvalidTransition, NotAuthorized
from apps.materials.models import SNCR, IntakeStatus, LabResult, MTCReading

from .base import MaterialsTestCase
from .helpers import LATER, bad_readings, good_readings


class MTCReviewTests(MaterialsTestCase):
    def test_in_spec_mtc_clears_a_normal_grade(self):
        i = self.receive()
        out = services.review_mtc(i, self.eng, good_readings(self.grade), LATER)
        self.assertEqual(out.status, IntakeStatus.CLEARED)
        self.assertEqual(out.cleared_at, LATER)
        self.assertEqual((out.mtc_verified_by, out.mtc_verified_at), (self.eng, LATER))
        self.assertEqual(i.readings.count(), 2)
        self.assertTrue(all(r.is_within_spec for r in i.readings.all()))
        self.assertTrue(AuditLog.objects.filter(field_name="status", new_value="CLEARED", user=self.eng).exists())

    def test_lab_grade_goes_to_lab_testing_and_alerts_quality(self):
        out = self.in_lab()
        self.assertEqual(out.status, IntakeStatus.IN_LAB_TESTING)
        self.assertIsNone(out.cleared_at)
        self.assertTrue(Notification.objects.filter(subject__startswith="Lab sample needed").exists())

    def test_out_of_spec_value_quarantines_and_raises_an_sncr(self):
        i = self.receive()
        out = services.review_mtc(i, self.eng, bad_readings(self.grade), LATER)
        self.assertEqual(out.status, IntakeStatus.SUPPLIER_QUARANTINE)
        sncr = out.sncr
        self.assertEqual(sncr.reason_code, SNCR.Reason.MTC_OUT_OF_SPEC)
        self.assertIn("Carbon", sncr.details)
        self.assertEqual(sncr.status, SNCR.Status.OPEN)
        carbon = i.readings.get(grade_property__name="Carbon")
        self.assertFalse(carbon.is_within_spec)
        self.assertEqual(carbon.spec_min, Decimal("0.36"))

    def test_limits_are_inclusive(self):
        i = self.receive()
        props = {p.name: p.pk for p in self.grade.properties.all()}
        out = services.review_mtc(i, self.eng, {props["Carbon"]: "0.44", props["Tensile strength"]: "700"}, LATER)
        self.assertEqual(out.status, IntakeStatus.CLEARED)

    def test_every_property_must_be_entered(self):
        i = self.receive()
        readings = good_readings(self.grade)
        readings.pop(next(iter(readings)))
        with self.assertRaises(IntakeRejected):
            services.review_mtc(i, self.eng, readings, LATER)
        self.assertFalse(i.readings.exists())
        i.refresh_from_db()
        self.assertEqual(i.status, IntakeStatus.RECEIVED)

    def test_non_numeric_value_refused(self):
        i = self.receive()
        readings = good_readings(self.grade)
        readings[next(iter(readings))] = "lots"
        with self.assertRaises(IntakeRejected):
            services.review_mtc(i, self.eng, readings, LATER)
        self.assertFalse(i.readings.exists())

    def test_receiver_cannot_verify_their_own_intake(self):
        from django.contrib.auth.models import Permission
        self.eng.user_permissions.add(Permission.objects.get(codename="create_intake"))
        i = self.receive(user=self.eng)
        with self.assertRaises(InvalidTransition):
            services.review_mtc(i, self.eng, good_readings(self.grade), LATER)

    def test_only_quality_may_review(self):
        i = self.receive()
        for user in (self.dock, self.lead, self.buyer, self.operator, self.sup):
            with self.assertRaises(NotAuthorized):
                services.review_mtc(i, user, good_readings(self.grade), LATER)

    def test_a_reviewed_lot_cannot_be_reviewed_again(self):
        i = self.cleared()
        with self.assertRaises(InvalidTransition):
            services.review_mtc(i, self.qm, good_readings(self.grade), LATER)

    def test_grade_without_limits_just_needs_confirmation(self):
        from .helpers import make_grade
        grade = make_grade("PLAIN", with_specs=False)
        out = self.cleared(grade=grade, heat="H-PLAIN")
        self.assertEqual(out.status, IntakeStatus.CLEARED)

    def test_the_limits_in_force_are_stored_with_the_reading(self):
        i = self.cleared()
        prop = self.grade.properties.get(name="Carbon")
        prop.max_value = Decimal("0.38")
        prop.save()
        reading = i.readings.get(grade_property=prop)
        self.assertTrue(reading.is_within_spec)
        self.assertEqual(reading.spec_max, Decimal("0.44"))
        self.assertIn("0.4400", reading.spec_display)


class LabTests(MaterialsTestCase):
    def test_pass_clears_the_lot(self):
        i = self.in_lab()
        out = services.record_lab_result(i, self.eng, passed=True, report_reference="LAB-9", now=LATER + timedelta(hours=3))
        self.assertEqual(out.status, IntakeStatus.CLEARED)
        self.assertTrue(LabResult.objects.get().passed)

    def test_fail_quarantines_with_an_sncr(self):
        i = self.in_lab()
        out = services.record_lab_result(i, self.qm, passed=False, report_reference="LAB-9",
                                         remarks="Spectro shows excess sulphur", now=LATER)
        self.assertEqual(out.status, IntakeStatus.SUPPLIER_QUARANTINE)
        self.assertEqual(out.sncr.reason_code, SNCR.Reason.LAB_FAIL)
        self.assertIn("sulphur", out.sncr.details)

    def test_failure_needs_a_description_and_a_report_reference(self):
        i = self.in_lab()
        with self.assertRaises(IntakeRejected):
            services.record_lab_result(i, self.eng, passed=False, report_reference="LAB-1", remarks="bad", now=LATER)
        with self.assertRaises(IntakeRejected):
            services.record_lab_result(i, self.eng, passed=True, report_reference=" ", now=LATER)

    def test_only_lots_in_lab_can_take_a_result(self):
        for i in (self.receive(heat="H-A"), self.cleared(heat="H-B")):
            with self.assertRaises(InvalidTransition):
                services.record_lab_result(i, self.eng, passed=True, report_reference="LAB-1", now=LATER)

    def test_only_quality_records_lab_results(self):
        i = self.in_lab()
        for user in (self.dock, self.buyer, self.lead):
            with self.assertRaises(NotAuthorized):
                services.record_lab_result(i, user, passed=True, report_reference="LAB-1", now=LATER)

    def test_result_is_recorded_once(self):
        i = self.in_lab()
        services.record_lab_result(i, self.eng, passed=True, report_reference="LAB-1", now=LATER)
        with self.assertRaises(InvalidTransition):
            services.record_lab_result(i, self.eng, passed=True, report_reference="LAB-2", now=LATER)
