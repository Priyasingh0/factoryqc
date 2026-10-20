"""One lot through every module with real records: nothing is inserted behind the services' back."""

from decimal import Decimal

from apps.dispatch import services as dispatch
from apps.dispatch.exceptions import ShippingInterlock
from apps.dispatch.models import ShippingDocument
from apps.dispatch.tests.helpers import AFTER, DispatchTestCase
from apps.handover import services as handover
from apps.inspections import services as inspections
from apps.inspections.tests.helpers import begin_pdi, build_part, enter
from apps.materials import services as materials
from apps.materials.exceptions import MaterialBlocked
from apps.materials.tests.helpers import good_readings, make_grade, make_supplier, pdf
from apps.plant.tests.helpers import make_machine
from apps.production import services as production
from apps.production.exceptions import ReleaseError
from apps.production.models import WorkOrder
from apps.production.tests.helpers import NOW, make_machine_ready, run_through
from apps.review import services as review
from apps.review.tests.helpers import GOOD, LATER


def stage(flow, code):
    return next(s for s in flow.stages if s.code == code)


class EndToEndTests(DispatchTestCase):
    def test_material_to_gate_pass_with_every_gate_enforced(self):
        part = build_part(self.engineer, self.manager, number="P-E2E")
        order = WorkOrder.objects.create(
            number="WO-E2E", part=part, quantity=2, heat_number="H-E2E-1", customer_po="PO-E", created_by=self.lead)

        # Materials -> Production: no cleared stock, no release.
        with self.assertRaises(ReleaseError):
            production.release_work_order(order, self.lead)
        grade, supplier = make_grade("E2E-G"), make_supplier()
        intake = materials.create_intake(
            self.dock, supplier=supplier, po_number="PO-M", grade=grade, heat_number="H-E2E-1",
            mtc_heat_number="H-E2E-1", quantity="100", unit="KG", mtc_upload=pdf(), now=NOW)
        with self.assertRaises(MaterialBlocked):
            materials.issue_material(order, intake, Decimal("2"), self.lead, NOW)   # not yet cleared
        with self.assertRaises(ReleaseError):
            production.release_work_order(order, self.lead)
        materials.review_mtc(intake, self.engineer, good_readings(grade), NOW)
        materials.issue_material(order, intake, Decimal("2"), self.lead, NOW)
        traveler = production.release_work_order(order, self.lead)

        flow = handover.flow_status(traveler, NOW)
        self.assertTrue(stage(flow, "MATERIAL").ok)
        self.assertFalse(stage(flow, "PRODUCTION").ok)
        self.assertEqual(flow.current.code, "PRODUCTION")

        # Production -> Inspection: three machined steps, then the final PDI.
        station = None
        for number, code, t in ((10, "CNC-10", NOW), (20, "CNC-20", NOW.replace(hour=11)), (30, "CNC-30", NOW.replace(hour=12))):
            station = make_machine_ready(code, t, self.operator)
            run_through(self, traveler, station, number, t, 2)
        flow = handover.flow_status(traveler, NOW.replace(hour=13))
        self.assertTrue(stage(flow, "PRODUCTION").ok)
        self.assertFalse(stage(flow, "FINAL_PDI").ok)
        with self.assertRaises(ShippingInterlock):
            dispatch.create_manifest(traveler, self.clerk, vehicle_number="MH-12-1", now=AFTER)

        # Inspection -> Review: submitted is not approved.
        insp = begin_pdi(self, traveler, station)
        for label, (value, gauge) in GOOD.items():
            enter(self, insp, label, value, gauge=gauge)
        insp = inspections.submit_inspection(insp, self.qa, now=NOW)
        self.assertFalse(handover.flow_status(traveler, AFTER).ready)
        with self.assertRaises(ShippingInterlock):
            dispatch.create_manifest(traveler, self.clerk, vehicle_number="MH-12-1", now=AFTER)
        review.approve_inspection(insp, self.supervisor, "", LATER)

        # Review -> Dispatch -> final shipment.
        flow = handover.flow_status(traveler, AFTER)
        self.assertTrue(flow.ready, flow.problems)
        manifest = dispatch.create_manifest(traveler, self.clerk, vehicle_number="MH-12-1", now=AFTER)
        gate_pass = dispatch.issue_gate_pass(manifest, self.clerk, AFTER)
        self.assertTrue(gate_pass.number.startswith("GP-"))
        dispatch.issue_document(manifest, ShippingDocument.Kind.PACKING_SLIP, self.clerk, AFTER)

        # A later recall of the heat locks every further document.
        materials.recall_lot(intake, self.qa_head, "Supplier advised heat has defective chemistry", AFTER)
        flow = handover.flow_status(traveler, AFTER)
        self.assertFalse(flow.ready)
        self.assertFalse(stage(flow, "MATERIAL").ok)
        with self.assertRaises(ShippingInterlock):
            dispatch.issue_document(manifest, ShippingDocument.Kind.INVOICE, self.clerk, AFTER)
