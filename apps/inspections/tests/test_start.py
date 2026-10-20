from datetime import timedelta

from apps.checklists.exceptions import TemplateNotAvailable
from apps.checklists.models import ChecklistType, TemplateStatus
from apps.checklists.tests.helpers import force_status
from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog
from apps.core.signing import QRKind, make_token
from apps.inspections import services
from apps.inspections.exceptions import CannotStart, NotAuthorized
from apps.inspections.models import Inspection, InspectionStatus
from apps.plant.tests.helpers import make_machine
from apps.production.exceptions import HoldActive
from apps.production.models import JobTraveler

from .helpers import (
    NOW, InspectionTestCase, begin_first_piece, begin_pdi, lot_in_setup, lot_ready_for_pdi, op_at,
)

FP = ChecklistType.FIRST_PIECE


class StartFirstPieceTests(InspectionTestCase):
    def setUp(self):
        self.traveler, self.machine = lot_in_setup(self)

    def start(self, **kw):
        args = dict(
            inspector=self.qa, machine_token=self.machine.qr_token,
            traveler_code=self.traveler.barcode, checklist_type=FP, now=NOW,
        )
        args.update(kw)
        return services.start_inspection(**args)

    def test_two_scans_open_the_released_template(self):
        inspection = self.start()
        self.assertEqual(inspection.status, InspectionStatus.IN_PROGRESS)
        self.assertEqual(inspection.template.operation_number, 10)
        self.assertEqual(inspection.template.status, TemplateStatus.RELEASED)
        self.assertEqual((inspection.machine, inspection.inspector), (self.machine, self.qa))
        self.assertEqual(inspection.traveler_operation, op_at(self.traveler, 10))

    def test_start_is_audited(self):
        inspection = self.start()
        self.assertTrue(AuditLog.objects.filter(
            action=AuditLog.Action.CREATE, model_name="inspections.Inspection",
            object_id=str(inspection.pk), user=self.qa).exists())

    def test_only_inspectors_may_start(self):
        for user in (self.operator, self.lead, self.supervisor, self.qa_head, self.dock):
            with self.assertRaises(NotAuthorized):
                self.start(inspector=user)
        self.assertFalse(Inspection.objects.exists())

    def test_forged_or_wrong_kind_station_code_rejected(self):
        for token in ("garbage", "MACHINE:1", make_token(QRKind.SHIPMENT, self.machine.pk), ""):
            with self.assertRaises(InvalidQRToken):
                self.start(machine_token=token)

    def test_forged_traveler_barcode_rejected(self):
        for code in ("garbage", self.machine.qr_token, ""):
            with self.assertRaises(InvalidQRToken):
                self.start(traveler_code=code)
        self.assertFalse(Inspection.objects.exists())

    def test_must_scan_the_machine_the_job_runs_on(self):
        other = make_machine("CNC-77")
        with self.assertRaisesMessage(CannotStart, "CNC-04"):
            self.start(machine_token=other.qr_token)

    def test_unknown_inspection_type_refused(self):
        for kind in (ChecklistType.IN_PROCESS, "NOPE", ""):
            with self.assertRaises(CannotStart):
                self.start(checklist_type=kind)

    def test_inactive_station_refused(self):
        self.machine.is_active = False
        self.machine.save()
        with self.assertRaises(CannotStart):
            self.start()

    def test_quarantined_lot_cannot_be_inspected(self):
        JobTraveler.objects.filter(pk=self.traveler.pk).update(status=JobTraveler.Status.QUARANTINED)
        with self.assertRaisesMessage(CannotStart, "Quarantined"):
            self.start()

    def test_inspector_is_locked_to_one_open_inspection(self):
        self.start()
        with self.assertRaisesMessage(CannotStart, "open inspection"):
            self.start()
        self.assertEqual(Inspection.objects.count(), 1)

    def test_a_second_inspector_cannot_open_the_same_step_twice(self):
        self.start()
        # qa2 has no open inspection of their own, but the step already has one.
        with self.assertRaisesMessage(CannotStart, "Another inspector"):
            self.start(inspector=self.qa2)

    def test_template_must_be_released(self):
        from apps.checklists.models import ChecklistTemplate
        template = ChecklistTemplate.objects.get(
            part=self.traveler.work_order.part, checklist_type=FP, operation_number=10)
        force_status(template, TemplateStatus.SUPERSEDED)
        with self.assertRaises(TemplateNotAvailable):
            self.start()

    def test_job_not_in_setup_cannot_have_a_first_piece_inspection(self):
        from apps.production.models import ProductionRun
        ProductionRun.objects.filter(traveler_operation=op_at(self.traveler, 10)).update(
            state=ProductionRun.State.RUNNING)
        with self.assertRaisesMessage(CannotStart, "not waiting for a first-piece"):
            self.start()


class StartPDITests(InspectionTestCase):
    def test_pdi_opens_after_all_steps_complete(self):
        traveler, station = lot_ready_for_pdi(self)
        inspection = begin_pdi(self, traveler, station)
        self.assertEqual(inspection.checklist_type, ChecklistType.PDI)
        self.assertTrue(inspection.traveler_operation.routing_operation.is_final_pdi)

    def test_pdi_refused_while_steps_unfinished(self):
        traveler, machine = lot_in_setup(self)
        with self.assertRaisesMessage(CannotStart, "Every production step"):
            services.start_inspection(self.qa, machine.qr_token, traveler.barcode, ChecklistType.PDI, NOW)

    def test_pdi_refused_during_cooling_hold(self):
        traveler, station = lot_ready_for_pdi(self)
        from apps.production.models import TravelerOperation
        TravelerOperation.objects.filter(pk=op_at(traveler, 30).pk).update(
            hold_until=NOW + timedelta(hours=9))
        with self.assertRaises(HoldActive):
            begin_pdi(self, traveler, station)

    def test_only_one_pdi_awaiting_a_decision(self):
        from apps.inspections.models import Inspection as I
        traveler, station = lot_ready_for_pdi(self)
        first = begin_pdi(self, traveler, station)
        first.status = InspectionStatus.FROZEN
        first.save()
        with self.assertRaisesMessage(CannotStart, "already awaiting"):
            begin_pdi(self, traveler, station, user=self.qa2)
