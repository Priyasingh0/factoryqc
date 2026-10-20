from datetime import timedelta

from apps.dispatch import services as dispatch
from apps.dispatch.tests.helpers import (
    AFTER, DispatchTestCase, approved_lot, conceded_lot, force_pdi_pending, frozen_lot, make_red,
    quarantined_lot, set_hold, set_traveler_status,
)
from apps.handover import services
from apps.materials import services as materials
from apps.materials.models import RawMaterialIntake
from apps.production.models import JobTraveler


def stage(flow, code):
    return next(s for s in flow.stages if s.code == code)


class FlowStatusTests(DispatchTestCase):
    def test_a_fully_cleared_lot_is_green_at_every_stage(self):
        flow = services.flow_status(approved_lot(self), AFTER)
        self.assertTrue(flow.ready)
        self.assertEqual([s.code for s in flow.stages],
                         ["MATERIAL", "PRODUCTION", "QUALITY", "FINAL_PDI", "DISPATCH"])
        self.assertTrue(all(s.ok for s in flow.stages))
        self.assertIsNone(flow.current)
        self.assertEqual(flow.problems, [])

    def test_a_skipped_operation_blocks_production_and_dispatch(self):
        lot = approved_lot(self)
        make_red(lot, 20)
        flow = services.flow_status(lot, AFTER)
        self.assertFalse(flow.ready)
        self.assertFalse(stage(flow, "PRODUCTION").ok)
        self.assertIn("Heat treatment", " ".join(stage(flow, "PRODUCTION").problems))
        self.assertFalse(stage(flow, "DISPATCH").ok)
        self.assertEqual(flow.current.code, "PRODUCTION")

    def test_a_running_hold_blocks_quality(self):
        lot = approved_lot(self)
        set_hold(lot, AFTER + timedelta(hours=2))
        flow = services.flow_status(lot, AFTER)
        self.assertFalse(stage(flow, "QUALITY").ok)
        self.assertTrue(stage(flow, "PRODUCTION").ok)
        self.assertFalse(flow.ready)

    def test_a_pending_final_pdi_blocks_that_stage_only(self):
        lot = approved_lot(self)
        force_pdi_pending(lot)
        flow = services.flow_status(lot, AFTER)
        self.assertFalse(stage(flow, "FINAL_PDI").ok)
        self.assertTrue(stage(flow, "PRODUCTION").ok and stage(flow, "QUALITY").ok)

    def test_a_quarantined_lot_is_blocked_with_the_reason(self):
        lot = quarantined_lot(self)
        flow = services.flow_status(lot, AFTER)
        self.assertFalse(flow.ready)
        self.assertFalse(stage(flow, "QUALITY").ok)

    def test_a_frozen_inspection_blocks(self):
        flow = services.flow_status(frozen_lot(self), AFTER)
        self.assertFalse(flow.ready)

    def test_recalled_material_blocks_the_material_stage_and_dispatch(self):
        lot = approved_lot(self)
        intake = RawMaterialIntake.objects.get()
        materials.recall_lot(intake, self.qa_head, "Supplier advised heat has defective chemistry", AFTER)
        flow = services.flow_status(lot, AFTER)
        self.assertFalse(stage(flow, "MATERIAL").ok)
        self.assertIn(intake.lot_number, stage(flow, "MATERIAL").problems[0])
        self.assertFalse(flow.ready)
        self.assertFalse(stage(flow, "DISPATCH").ok)

    def assert_consistent(self, lot):
        """The flow and the dispatch interlock must always agree (nothing the interlock stops looks green)."""
        flow = services.flow_status(lot, AFTER)
        clearance = dispatch.clearance(lot, AFTER)
        self.assertEqual(flow.ready, clearance.cleared)
        self.assertTrue(set(clearance.problems) <= set(flow.problems))
        self.assertEqual(flow.ready, not flow.problems)
        return flow

    def test_flow_agrees_with_dispatch_for_a_cleared_lot(self):
        self.assertTrue(self.assert_consistent(approved_lot(self)).ready)

    def test_flow_agrees_when_op_10_is_unsigned(self):
        lot = approved_lot(self)
        make_red(lot, 10)
        self.assertFalse(self.assert_consistent(lot).ready)

    def test_flow_agrees_when_op_30_is_unsigned(self):
        lot = approved_lot(self)
        make_red(lot, 30)
        self.assertFalse(self.assert_consistent(lot).ready)

    def test_flow_agrees_when_the_final_pdi_step_is_unsigned(self):
        lot = approved_lot(self)
        make_red(lot, 40)
        flow = self.assert_consistent(lot)
        self.assertFalse(stage(flow, "FINAL_PDI").ok)
        self.assertTrue(stage(flow, "PRODUCTION").ok)

    def test_flow_agrees_when_a_hold_is_about_to_expire(self):
        lot = approved_lot(self)
        set_hold(lot, AFTER + timedelta(minutes=5))
        self.assertFalse(self.assert_consistent(lot).ready)

    def test_flow_agrees_for_pending_and_frozen_final_pdi(self):
        lot = approved_lot(self)
        force_pdi_pending(lot)
        self.assertFalse(self.assert_consistent(lot).ready)
        force_pdi_pending(lot, "FROZEN")
        self.assertFalse(self.assert_consistent(lot).ready)

    def test_flow_agrees_for_a_quarantined_traveler_status(self):
        lot = approved_lot(self)
        set_traveler_status(lot, JobTraveler.Status.QUARANTINED)
        self.assertFalse(self.assert_consistent(lot).ready)
        set_traveler_status(lot, JobTraveler.Status.CLOSED)
        self.assertFalse(self.assert_consistent(lot).ready)

    def test_flow_agrees_for_conceded_quarantined_and_frozen_lots(self):
        self.assertTrue(self.assert_consistent(conceded_lot(self)).ready)

    def test_flow_agrees_for_quarantined_lot(self):
        self.assertFalse(self.assert_consistent(quarantined_lot(self)).ready)

    def test_flow_agrees_for_frozen_lot(self):
        self.assertFalse(self.assert_consistent(frozen_lot(self)).ready)

    def test_a_concession_release_is_a_valid_clearance(self):
        self.assertTrue(services.flow_status(conceded_lot(self), AFTER).ready)
