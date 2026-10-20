from apps.dispatch import services as dispatch
from apps.dispatch.tests.helpers import AFTER, DispatchTestCase, approved_lot
from apps.materials import services
from apps.materials.models import IntakeStatus, RawMaterialIntake
from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.production import services as production
from apps.production.exceptions import OperationBlocked


class DispatchAndTraceLinkTests(DispatchTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.qh2 = make_user("qh_mat", Role.QUALITY_HEAD)

    def test_recalled_material_locks_a_finished_lot_for_dispatch(self):
        lot = approved_lot(self)
        self.assertTrue(dispatch.clearance(lot, AFTER).cleared)
        intake = RawMaterialIntake.objects.get()
        services.recall_lot(intake, self.qh2, "Supplier advised heat has defective chemistry", AFTER)
        result = dispatch.clearance(lot, AFTER)
        self.assertFalse(result.cleared)
        holds = next(c for c in result.checks if c.code == "HOLDS")
        self.assertIn(intake.lot_number, holds.problems[0])

    def test_returned_or_scrapped_material_also_locks(self):
        lot = approved_lot(self)
        intake = RawMaterialIntake.objects.get()
        services.recall_lot(intake, self.qh2, "Supplier advised heat has defective chemistry", AFTER)
        services.resolve_sncr(intake.sncr, self.qh2, "SCRAP", "CC-1", AFTER)
        self.assertFalse(dispatch.clearance(lot, AFTER).cleared)

    def test_final_pdi_cannot_be_approved_on_quarantined_material(self):
        from apps.review import services as review
        from apps.review.tests.helpers import LATER, pdi_pass
        insp, traveler = pdi_pass(self)
        intake = RawMaterialIntake.objects.get()
        services.recall_lot(intake, self.qh2, "Supplier advised heat has defective chemistry", LATER)
        with self.assertRaises(OperationBlocked):
            review.approve_inspection(insp, self.supervisor, "", LATER)

    def test_certificate_shows_the_material(self):
        lot = approved_lot(self)
        m = dispatch.create_manifest(lot, self.clerk, vehicle_number="V1", now=AFTER)
        label = dispatch.issue_label(m, self.clerk, AFTER)
        data = dispatch.certificate(label.token)
        self.assertEqual([i.heat_number for i in data["lots"]], ["H-7788"])

    def test_birth_certificate_lists_the_lot(self):
        lot = approved_lot(self)
        serial = lot.serials.first().serial_number
        birth = production.backward_trace(serial)
        self.assertEqual(len(birth["lots"]), 1)
