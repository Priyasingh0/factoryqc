from apps.checklists.models import ChecklistType
from apps.core.models import AuditLog
from apps.production import services
from apps.production.exceptions import NotAuthorized, RecordLockedError, ReleaseError
from apps.production.models import JobTraveler, TravelerOperation, WorkOrder

from .helpers import (
    ProductionTestCase, make_ready_part, make_routing, make_work_order, release_template,
)
from apps.checklists.tests.helpers import make_part


class ReleaseTests(ProductionTestCase):
    def setUp(self):
        self.part = make_ready_part(self.engineer, self.manager)
        self.wo = make_work_order(self.lead, self.part)

    def test_release_generates_traveler_with_every_routing_step(self):
        traveler = services.release_work_order(self.wo, self.lead)
        self.assertEqual(traveler.quantity, 5)
        self.assertEqual(traveler.status, JobTraveler.Status.ACTIVE)
        steps = list(traveler.operations.order_by("routing_operation__op_number")
                     .values_list("routing_operation__op_number", "status"))
        self.assertEqual(steps, [(10, "PENDING"), (20, "PENDING"), (30, "PENDING"), (40, "PENDING")])
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.released_by, self.lead)
        self.assertTrue(self.wo.is_released)

    def test_release_is_audited(self):
        services.release_work_order(self.wo, self.lead)
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.Action.DECISION, user=self.lead, field_name="released_at").exists())

    def test_only_roles_with_release_permission(self):
        for user in (self.operator, self.qa, self.engineer, self.manager):
            with self.assertRaises(NotAuthorized):
                services.release_work_order(self.wo, user)
        self.assertFalse(JobTraveler.objects.exists())

    def test_heat_number_required(self):
        self.wo.heat_number = " "
        self.wo.save()
        with self.assertRaisesMessage(ReleaseError, "heat number"):
            services.release_work_order(self.wo, self.lead)

    def test_part_without_routing_blocks(self):
        wo = make_work_order(self.lead, make_part("P-EMPTY"), number="WO-2")
        with self.assertRaisesMessage(ReleaseError, "no routing"):
            services.release_work_order(wo, self.lead)

    def test_missing_released_first_piece_template_blocks(self):
        part = make_part("P-NOTPL")
        make_routing(part)
        release_template(self.engineer, self.manager, part, ChecklistType.PDI)
        wo = make_work_order(self.lead, part, number="WO-3")
        with self.assertRaises(ReleaseError) as ctx:
            services.release_work_order(wo, self.lead)
        self.assertIn("Op 10", str(ctx.exception))
        self.assertIn("Op 30", str(ctx.exception))
        self.assertFalse(TravelerOperation.objects.exists())

    def test_missing_pdi_template_blocks(self):
        part = make_part("P-NOPDI")
        make_routing(part)
        release_template(self.engineer, self.manager, part, ChecklistType.FIRST_PIECE, 10)
        release_template(self.engineer, self.manager, part, ChecklistType.FIRST_PIECE, 30)
        wo = make_work_order(self.lead, part, number="WO-4")
        with self.assertRaisesMessage(ReleaseError, "Op 40"):
            services.release_work_order(wo, self.lead)

    def test_final_pdi_must_be_last(self):
        from apps.production.models import RoutingOperation
        part = make_part("P-ORDER")
        RoutingOperation.objects.create(part=part, op_number=10, name="PDI", is_final_pdi=True)
        RoutingOperation.objects.create(part=part, op_number=20, name="Turn")
        wo = make_work_order(self.lead, part, number="WO-5")
        self.assertTrue(any("last" in p for p in services.release_problems(wo)))

    def test_cannot_release_twice(self):
        services.release_work_order(self.wo, self.lead)
        with self.assertRaises(ReleaseError):
            services.release_work_order(self.wo, self.lead)
        self.assertEqual(JobTraveler.objects.count(), 1)

    def test_released_work_order_is_locked(self):
        services.release_work_order(self.wo, self.lead)
        self.wo.refresh_from_db()
        self.wo.quantity = 99
        with self.assertRaises(RecordLockedError):
            self.wo.save()
        with self.assertRaises(RecordLockedError):
            self.wo.delete()
        with self.assertRaises(RecordLockedError):
            WorkOrder.objects.filter(pk=self.wo.pk).update(quantity=1)
        with self.assertRaises(RecordLockedError):
            WorkOrder.objects.filter(pk=self.wo.pk).delete()

    def test_unreleased_work_order_can_still_be_edited(self):
        self.wo.quantity = 9
        self.wo.save()
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.quantity, 9)

    def test_routing_locked_once_on_a_traveler(self):
        from apps.production.models import RoutingOperation
        services.release_work_order(self.wo, self.lead)
        step = RoutingOperation.objects.get(part=self.part, op_number=20)
        step.hold_minutes = 1
        with self.assertRaises(RecordLockedError):
            step.save()

    def test_traveler_barcode_round_trips(self):
        traveler = services.release_work_order(self.wo, self.lead)
        self.assertEqual(services.resolve_traveler_from_barcode(traveler.barcode), traveler)
