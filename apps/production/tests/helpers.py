from datetime import timedelta
from io import StringIO

from django.core.management import call_command

from apps.accounts.models import Role
from apps.accounts.tests.helpers import make_user
from apps.checklists.models import ChecklistTemplate, ChecklistType, TemplateStatus
from apps.inspections.models import Inspection, InspectionStatus
from apps.checklists.tests.helpers import MediaTestCase, force_status, make_part, make_template
from apps.plant.services import record_pm_check
from apps.plant.tests.helpers import at, make_machine
from apps.production import services
from apps.production.models import ProductionRun, RoutingOperation, WorkOrder

NOW = at(2026, 3, 10, 10, 0)  # Tuesday, morning shift


class ProductionTestCase(MediaTestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("setup_roles", stdout=StringIO())
        cls.lead = make_user("lead", Role.PRODUCTION_LEAD)
        cls.operator = make_user("op1", Role.MACHINE_OPERATOR)
        cls.operator2 = make_user("op2", Role.MACHINE_OPERATOR)
        cls.qa = make_user("qa1", Role.QA_INSPECTOR)
        cls.qa2 = make_user("qa2", Role.QA_INSPECTOR)
        cls.engineer = make_user("eng", Role.QUALITY_ENGINEER)
        cls.manager = make_user("qm", Role.QUALITY_MANAGER)
        cls.plant_manager = make_user("pm", Role.PLANT_MANAGER)
        cls.dock = make_user("dock", Role.DOCK_CLERK)


def make_routing(part, *, op10_hold=0, op20_hold=90, interval=None, every_parts=None):
    """Op 10 turning (first piece), 20 heat treatment (hold), 30 grinding (first piece), 40 final PDI."""
    RoutingOperation.objects.create(
        part=part, op_number=10, name="CNC turning", hold_minutes=op10_hold,
        requires_first_piece=True, inspection_interval_minutes=interval,
        inspection_every_parts=every_parts,
    )
    RoutingOperation.objects.create(part=part, op_number=20, name="Heat treatment", hold_minutes=op20_hold)
    RoutingOperation.objects.create(part=part, op_number=30, name="Grinding", requires_first_piece=True)
    RoutingOperation.objects.create(part=part, op_number=40, name="Final PDI", is_final_pdi=True)


def release_template(user, approver, part, checklist_type, operation_number=None):
    t = make_template(user, part=part, checklist_type=checklist_type, operation_number=operation_number)
    return force_status(t, TemplateStatus.RELEASED, approver)


def make_ready_part(user, approver, number="P-200", **routing):
    """A part with routing plus every Released template a release needs."""
    part = make_part(number)
    make_routing(part, **routing)
    release_template(user, approver, part, ChecklistType.FIRST_PIECE, 10)
    release_template(user, approver, part, ChecklistType.FIRST_PIECE, 30)
    release_template(user, approver, part, ChecklistType.PDI)
    return part


def make_work_order(user, part, number="WO-1", quantity=5, heat="H-7788", issue_material=True):
    """A work order. Since Step 8 a release needs cleared raw material issued to it, so by default it is
    given a cleared lot of its heat number (pass issue_material=False to test the missing-stock rules)."""
    from apps.materials.tests.helpers import cleared_lot_for

    work_order = WorkOrder.objects.create(
        number=number, part=part, quantity=quantity, heat_number=heat,
        customer_po="PO-1", created_by=user,
    )
    if issue_material and heat.strip():
        cleared_lot_for(work_order, user)
    return work_order


def make_machine_ready(code="CNC-04", now=NOW, user=None):
    machine = make_machine(code)
    record_pm_check(machine, user, completed=True, safety_zero_point_ok=True, now=now)
    return machine


def released_traveler(case, quantity=5, ready_at=NOW, **routing):
    """Released work order -> traveler, plus a machine PM-ready at ready_at. Returns (traveler, machine)."""
    part = make_ready_part(case.engineer, case.manager, **routing)
    wo = make_work_order(case.lead, part, quantity=quantity)
    traveler = services.release_work_order(wo, case.lead)
    return traveler, make_machine_ready(now=ready_at, user=case.operator)


def op_at(traveler, number):
    return traveler.operations.get(routing_operation__op_number=number)


def run_through(case, traveler, machine, number, now, parts=None):
    """Start, clear first piece (if needed), count everything and sign off one step."""
    op = op_at(traveler, number)
    services.start_operation(op, case.operator, machine, now)
    op.refresh_from_db()
    if op.routing_operation.requires_first_piece:
        sign_off(op.run, case.qa, now)
    services.record_parts(op, case.operator, parts or traveler.quantity, now)
    return services.complete_operation(op, case.operator, now + timedelta(minutes=30))


def attach_passed_first_piece(run, inspector):
    """Give the run a submitted, fully in-tolerance first-piece inspection (the Step 5 hook).

    Inserted directly: production tests are about production rules. The real
    inspection workflow is tested in apps/inspections.
    """
    operation = run.traveler_operation
    template = ChecklistTemplate.objects.get(
        part=operation.traveler.work_order.part, checklist_type=ChecklistType.FIRST_PIECE,
        operation_number=operation.routing_operation.op_number, status=TemplateStatus.RELEASED,
    )
    inspection = Inspection.objects.create(
        template=template, checklist_type=ChecklistType.FIRST_PIECE, traveler=operation.traveler,
        traveler_operation=operation, machine=operation.machine, inspector=inspector,
        status=InspectionStatus.SUBMITTED, submitted_at=operation.clock_in,
    )
    run.first_piece_inspection = inspection
    run.save(update_fields=["first_piece_inspection"])
    return inspection


def sign_off(run, inspector, now=NOW):
    """QA first-piece sign-off, attaching a passed inspection first when the run has none."""
    run = ProductionRun.objects.get(pk=run.pk)
    if run.first_piece_inspection_id is None:
        attach_passed_first_piece(run, inspector)
    return services.clear_first_piece(run, inspector, now)
