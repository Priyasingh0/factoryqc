from apps.core.models import AuditLog, Notification
from apps.materials import services
from apps.materials.exceptions import IntakeRejected, InvalidTransition, NotAuthorized
from apps.materials.models import SNCR, IntakeStatus

from .base import MaterialsTestCase
from .helpers import LATER


class RejectIntakeTests(MaterialsTestCase):
    def test_reject_quarantines_logs_an_sncr_and_notifies_purchasing_and_quality(self):
        i = self.receive()
        sncr = services.reject_intake(i, self.dock, "NO_VALID_MTC", "The certificate is illegible", LATER)
        i.refresh_from_db()
        self.assertEqual(i.status, IntakeStatus.SUPPLIER_QUARANTINE)
        self.assertEqual((sncr.intake, sncr.raised_by, sncr.status), (i, self.dock, SNCR.Status.OPEN))
        self.assertTrue(sncr.number.startswith("SNCR-"))
        roles = set(Notification.objects.filter(related_model="materials.SNCR", related_object_id=str(sncr.pk)).values_list("recipient_role", flat=True))
        self.assertEqual(roles, {"PURCHASING", "QUALITY_MANAGER", "QUALITY_HEAD"})
        self.assertTrue(AuditLog.objects.filter(model_name="materials.SNCR", action=AuditLog.Action.CREATE).exists())
        self.assertTrue(AuditLog.objects.filter(field_name="status", new_value="SUPPLIER_QUARANTINE").exists())

    def test_a_lot_in_lab_testing_can_be_rejected(self):
        i = self.in_lab()
        services.reject_intake(i, self.qm, "SPECTRO", "Spectro shows wrong alloy", LATER)
        i.refresh_from_db()
        self.assertEqual(i.status, IntakeStatus.SUPPLIER_QUARANTINE)

    def test_cleared_stock_cannot_be_rejected_at_intake(self):
        i = self.cleared()
        with self.assertRaises(InvalidTransition):
            services.reject_intake(i, self.dock, "DIMENSION", "Bars are bent beyond tolerance", LATER)

    def test_reason_and_description_are_required(self):
        i = self.receive()
        for code, text in (("", "Bars are bent beyond tolerance"), ("RECALL", "Bars are bent beyond tolerance"),
                           ("DIMENSION", "bent"), ("DIMENSION", "   ")):
            with self.assertRaises(IntakeRejected):
                services.reject_intake(i, self.dock, code, text, LATER)
        i.refresh_from_db()
        self.assertEqual(i.status, IntakeStatus.RECEIVED)

    def test_only_dock_and_quality_may_reject(self):
        i = self.receive()
        for user in (self.lead, self.buyer, self.operator, self.pm, self.sup):
            with self.assertRaises(NotAuthorized):
                services.reject_intake(i, user, "DIMENSION", "Bars are bent beyond tolerance", LATER)

    def test_a_rejected_lot_cannot_be_rejected_twice(self):
        i = self.quarantined()
        with self.assertRaises(InvalidTransition):
            services.reject_intake(i, self.dock, "DIMENSION", "Bars are bent beyond tolerance", LATER)

    def test_a_quarantined_lot_can_never_be_cleared(self):
        i = self.quarantined()
        with self.assertRaises(InvalidTransition):
            services.review_mtc(i, self.eng, {}, LATER)
        with self.assertRaises(InvalidTransition):
            services.record_lab_result(i, self.eng, passed=True, report_reference="LAB-1", now=LATER)


class RecallTests(MaterialsTestCase):
    def test_a_cleared_lot_can_be_quarantined_by_quality_head_or_manager(self):
        for user, heat in ((self.qh, "H-A"), (self.qm, "H-B")):
            i = self.cleared(heat=heat)
            sncr = services.recall_lot(i, user, "Supplier advised heat has defective chemistry", LATER)
            i.refresh_from_db()
            self.assertEqual(i.status, IntakeStatus.SUPPLIER_QUARANTINE)
            self.assertEqual(sncr.reason_code, SNCR.Reason.RECALL)

    def test_only_cleared_stock_and_only_the_right_roles(self):
        i = self.cleared()
        for user in (self.dock, self.eng, self.lead, self.buyer):
            with self.assertRaises(NotAuthorized):
                services.recall_lot(i, user, "Supplier advised heat has defective chemistry", LATER)
        raw = self.receive(heat="H-RAW")
        with self.assertRaises(InvalidTransition):
            services.recall_lot(raw, self.qh, "Supplier advised heat has defective chemistry", LATER)

    def test_explanation_required(self):
        i = self.cleared()
        with self.assertRaises(IntakeRejected):
            services.recall_lot(i, self.qh, "bad heat", LATER)


class ResolveSncrTests(MaterialsTestCase):
    def test_return_to_vendor_closes_the_sncr_and_tells_the_dock(self):
        i = self.quarantined()
        sncr = services.resolve_sncr(i.sncr, self.buyer, "RTV", "DN-4471", LATER)
        i.refresh_from_db()
        self.assertEqual(i.status, IntakeStatus.RETURNED)
        self.assertEqual((sncr.status, sncr.resolution, sncr.resolution_reference), ("CLOSED", "RTV", "DN-4471"))
        self.assertEqual((sncr.closed_by, sncr.closed_at), (self.buyer, LATER))
        self.assertTrue(Notification.objects.filter(recipient_role="DOCK_CLERK", subject__startswith="Return to vendor").exists())
        self.assertTrue(AuditLog.objects.filter(model_name="materials.SNCR", field_name="status", new_value="CLOSED").exists())

    def test_scrap_needs_a_cost_code_and_ends_scrapped(self):
        i = self.quarantined()
        with self.assertRaises(InvalidTransition):
            services.resolve_sncr(i.sncr, self.qh, "SCRAP", "  ", LATER)
        services.resolve_sncr(i.sncr, self.qh, "SCRAP", "CC-900", LATER)
        i.refresh_from_db()
        self.assertEqual(i.status, IntakeStatus.SCRAPPED)

    def test_cannot_resolve_twice_or_with_a_bad_choice(self):
        i = self.quarantined()
        with self.assertRaises(InvalidTransition):
            services.resolve_sncr(i.sncr, self.buyer, "USE_AS_IS", "x", LATER)
        services.resolve_sncr(i.sncr, self.buyer, "RTV", "DN-1", LATER)
        with self.assertRaises(InvalidTransition):
            services.resolve_sncr(i.sncr, self.buyer, "SCRAP", "CC-1", LATER)

    def test_only_purchasing_and_quality_head_resolve(self):
        i = self.quarantined()
        for user in (self.dock, self.eng, self.qm, self.lead, self.pm):
            with self.assertRaises(NotAuthorized):
                services.resolve_sncr(i.sncr, user, "RTV", "DN-1", LATER)
