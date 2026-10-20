from datetime import timedelta
from decimal import Decimal

from apps.core.models import AuditLog, Notification
from apps.dispatch import services as dispatch
from apps.inspections.tests.helpers import build_part
from apps.materials import services
from apps.materials.exceptions import MaterialBlocked, MaterialsError, NotAuthorized
from apps.materials.models import IntakeStatus, MaterialIssue
from apps.production import services as production
from apps.production.exceptions import OperationBlocked, ReleaseError
from apps.production.tests.helpers import NOW, make_machine_ready, make_routing, make_work_order, op_at

from .base import MaterialsTestCase
from .helpers import LATER

from apps.checklists.tests.helpers import make_part


class WorkOrderMixin:
    def work_order(self, number="WO-M1", heat="H-1001", quantity=2, issue_material=False):
        part = build_part(self.eng, self.qm, number=f"P-{number}")
        return make_work_order(self.lead, part, number=number, quantity=quantity, heat=heat, issue_material=issue_material)


class IssueTests(WorkOrderMixin, MaterialsTestCase):
    def test_cleared_lot_is_issued_and_audited(self):
        lot, wo = self.cleared(), self.work_order()
        issue = services.issue_material(wo, lot, "120.5", self.lead, LATER)
        self.assertEqual((issue.intake, issue.work_order, issue.quantity), (lot, wo, Decimal("120.5")))
        self.assertEqual(lot.remaining_quantity, Decimal("379.5"))
        row = AuditLog.objects.get(model_name="materials.MaterialIssue", action=AuditLog.Action.CREATE)
        self.assertIn(wo.number, row.remarks)
        self.assertEqual(row.user, self.lead)

    def test_every_non_cleared_status_is_refused_and_the_attempt_audited(self):
        wo = self.work_order()
        lots = {
            "RECEIVED": self.receive(heat="H-1001", po="PO-1"),
            "IN_LAB_TESTING": self.in_lab(heat="H-1001", po="PO-2"),
            "SUPPLIER_QUARANTINE": self.quarantined(heat="H-1001", po="PO-3"),
        }
        for status, lot in lots.items():
            with self.assertRaises(MaterialBlocked, msg=status):
                services.issue_material(wo, lot, "10", self.lead, LATER)
        returned = self.quarantined(heat="H-1001", po="PO-4")
        services.resolve_sncr(returned.sncr, self.buyer, "RTV", "DN-1", LATER)
        scrapped = self.quarantined(heat="H-1001", po="PO-5")
        services.resolve_sncr(scrapped.sncr, self.qh, "SCRAP", "CC-1", LATER)
        for lot in (returned, scrapped):
            with self.assertRaises(MaterialBlocked):
                services.issue_material(wo, lot, "10", self.lead, LATER)
        self.assertFalse(MaterialIssue.objects.exists())
        self.assertEqual(AuditLog.objects.filter(field_name="issue_blocked").count(), 5)

    def test_heat_number_must_match_the_work_order(self):
        lot, wo = self.cleared(), self.work_order(heat="H-OTHER")
        with self.assertRaises(MaterialsError):
            services.issue_material(wo, lot, "10", self.lead, LATER)

    def test_quantity_rules(self):
        lot, wo = self.cleared(qty="100"), self.work_order()
        for qty in ("0", "-1", "abc", "NaN"):
            with self.assertRaises(MaterialsError, msg=qty):
                services.issue_material(wo, lot, qty, self.lead, LATER)
        with self.assertRaises(MaterialsError):
            services.issue_material(wo, lot, "100.001", self.lead, LATER)
        services.issue_material(wo, lot, "100", self.lead, LATER)

    def test_stock_cannot_be_overissued_across_work_orders(self):
        lot = self.cleared(qty="100")
        services.issue_material(self.work_order("WO-A"), lot, "70", self.lead, LATER)
        with self.assertRaises(MaterialsError):
            services.issue_material(self.work_order("WO-B"), lot, "40", self.lead, LATER)

    def test_same_lot_once_per_work_order(self):
        lot, wo = self.cleared(), self.work_order()
        services.issue_material(wo, lot, "10", self.lead, LATER)
        with self.assertRaises(MaterialsError):
            services.issue_material(wo, lot, "10", self.lead, LATER)

    def test_released_work_order_cannot_take_more_material(self):
        lot, wo = self.cleared(), self.work_order()
        services.issue_material(wo, lot, "10", self.lead, LATER)
        production.release_work_order(wo, self.lead)
        lot2 = self.cleared(po="PO-79")
        with self.assertRaises(MaterialsError):
            services.issue_material(wo, lot2, "10", self.lead, LATER)

    def test_only_production_lead_and_plant_manager_issue(self):
        lot, wo = self.cleared(), self.work_order()
        for user in (self.dock, self.eng, self.buyer, self.operator, self.sup):
            with self.assertRaises(NotAuthorized):
                services.issue_material(wo, lot, "10", user, LATER)
        self.assertTrue(self.lead.has_perm(services.PERM_ISSUE))

    def test_bundle_barcode_resolves_to_the_lot(self):
        lot = self.cleared()
        self.assertEqual(services.resolve_intake_from_barcode(lot.barcode), lot)
        for code in ("", "garbage", lot.barcode[:-3] + "abc"):
            with self.assertRaises(MaterialsError):
                services.resolve_intake_from_barcode(code)
        from apps.core.signing import QRKind, make_token
        with self.assertRaises(MaterialsError):
            services.resolve_intake_from_barcode(make_token(QRKind.MACHINE, lot.pk))


class ReleaseRuleTests(WorkOrderMixin, MaterialsTestCase):
    def test_release_needs_material_issued(self):
        wo = self.work_order()
        with self.assertRaises(ReleaseError) as ctx:
            production.release_work_order(wo, self.lead)
        self.assertIn("Issue raw material", ctx.exception.problems[0])
        services.issue_material(wo, self.cleared(), "10", self.lead, LATER)
        self.assertTrue(production.release_work_order(wo, self.lead))

    def test_release_refused_if_the_issued_lot_was_quarantined_meanwhile(self):
        wo, lot = self.work_order(), self.cleared()
        services.issue_material(wo, lot, "10", self.lead, LATER)
        services.recall_lot(lot, self.qh, "Supplier advised heat has defective chemistry", LATER)
        with self.assertRaises(ReleaseError) as ctx:
            production.release_work_order(wo, self.lead)
        self.assertIn("quarantined or rejected material cannot be used", " ".join(ctx.exception.problems))

    def test_release_refused_if_the_heat_number_was_changed_after_issue(self):
        wo, lot = self.work_order(), self.cleared()
        services.issue_material(wo, lot, "10", self.lead, LATER)
        wo.heat_number = "H-9999"
        wo.save()
        with self.assertRaises(ReleaseError) as ctx:
            production.release_work_order(wo, self.lead)
        self.assertIn("H-1001", " ".join(ctx.exception.problems))

    def test_blank_heat_number_still_asks_only_for_the_heat_number(self):
        wo = self.work_order(heat=" ")
        self.assertEqual(len([p for p in production.release_problems(wo) if "heat" in p.lower()]), 1)
        self.assertFalse(any("Issue raw material" in p for p in production.release_problems(wo)))


class ConsumptionBlockedTests(WorkOrderMixin, MaterialsTestCase):
    def released(self):
        wo, lot = self.work_order(), self.cleared()
        services.issue_material(wo, lot, "10", self.lead, LATER)
        return production.release_work_order(wo, self.lead), lot

    def start_first_op(self, traveler, now=NOW):
        machine = make_machine_ready("CNC-M1", now, self.operator)
        return production.start_operation(op_at(traveler, 10), self.operator, machine, now)

    def test_cleared_material_lets_production_start(self):
        traveler, _ = self.released()
        self.assertEqual(self.start_first_op(traveler).status, "IN_PROGRESS")

    def test_quarantined_material_blocks_the_first_operation(self):
        traveler, lot = self.released()
        services.recall_lot(lot, self.qh, "Supplier advised heat has defective chemistry", LATER)
        with self.assertRaises(OperationBlocked) as ctx:
            self.start_first_op(traveler)
        self.assertIn(lot.lot_number, str(ctx.exception))
        self.assertIn("Supplier quarantine".lower(), str(ctx.exception).lower())

    def test_work_already_running_stops_when_material_is_quarantined(self):
        traveler, lot = self.released()
        op = self.start_first_op(traveler)
        services.recall_lot(lot, self.qh, "Supplier advised heat has defective chemistry", LATER)
        with self.assertRaises(OperationBlocked):
            production.record_parts(op, self.operator, 1, NOW + timedelta(minutes=5))
        with self.assertRaises(OperationBlocked):
            production.complete_operation(op, self.operator, NOW + timedelta(minutes=10))


class RecallImpactTests(WorkOrderMixin, MaterialsTestCase):
    def test_recall_alerts_production_with_the_affected_orders(self):
        lot = self.cleared()
        wo = self.work_order()
        services.issue_material(wo, lot, "10", self.lead, LATER)
        sncr = services.recall_lot(lot, self.qh, "Supplier advised heat has defective chemistry", LATER)
        note = Notification.objects.get(recipient_role="PRODUCTION_LEAD", related_model="materials.SNCR", related_object_id=str(sncr.pk))
        self.assertIn(wo.number, note.body)
        self.assertEqual(services.affected_work_orders(lot), [wo])

    def test_no_production_alert_when_nothing_was_issued(self):
        sncr = services.recall_lot(self.cleared(), self.qh, "Supplier advised heat has defective chemistry", LATER)
        self.assertFalse(Notification.objects.filter(recipient_role="PRODUCTION_LEAD", related_model="materials.SNCR", related_object_id=str(sncr.pk)).exists())

    def test_forward_trace_lists_every_lot_of_a_heat(self):
        a = self.cleared(po="PO-1")
        b = self.cleared(po="PO-2")
        self.assertEqual({x.pk for x in services.lots_for_heat("h-1001")}, {a.pk, b.pk})
        self.assertEqual(services.lots_for_heat("nope"), [])


