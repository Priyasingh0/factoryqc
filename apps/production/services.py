"""Production business rules (PRD 7, 8, 9). Views call these; they hold no HTTP logic.

Status of the hooks for later steps:
  * Final PDI step: cannot be started or completed here. Step 6 completes it
    when the supervisor approves the PDI inspection.
  * First-piece sign-off: QA signs off here, and only after the inspections app
    has linked a submitted, fully in-tolerance first-piece inspection to the run.
  * Raw material (Step 8): release needs cleared stock issued to the work order, and no work
    may start or finish on a lot whose material has since been quarantined.
"""

from dataclasses import dataclass
from datetime import timedelta
from math import ceil

from django.db import transaction
from django.utils import timezone

from apps.checklists.exceptions import TemplateNotAvailable
from apps.checklists.models import ChecklistType
from apps.checklists.services import get_active_template
from apps.core.audit import log_event
from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog
from apps.core.signing import QRKind, read_token
from apps.handover import services as handover
from apps.inspections.models import InspectionStatus
from apps.materials import services as materials
from apps.plant.services import assert_machine_ready, resolve_machine_from_qr, shift_for

from .exceptions import (
    FirstPieceLocked,
    HoldActive,
    NotAuthorized,
    OperationBlocked,
    ReleaseError,
)
from .models import (
    JobTraveler,
    PartSerial,
    ProductionRun,
    TravelerOperation,
    WorkOrder,
)

PERM_RELEASE = "production.release_workorder"
PERM_OPERATE = "production.operate_traveleroperation"
PERM_CLEAR_FIRST_PIECE = "production.clear_first_piece"
PERM_LOG_CHECK = "production.log_periodic_check"

# PRD 9.2: the operator has 15 minutes after "Periodic Quality Inspection Due"
# before the job timer pauses.
INSPECTION_GRACE = timedelta(minutes=15)

OP = TravelerOperation.Status


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


def format_remaining(delta):
    """'1 hr 22 min' (PRD 8.2). Rounds up so it never shows 0 min while time is left."""
    total_minutes = max(0, ceil(delta.total_seconds() / 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} hr {minutes} min" if hours else f"{minutes} min"


def _audit(user, instance, field, previous, new, action=AuditLog.Action.OTHER, remarks=""):
    log_event(
        action=action, user=user, instance=instance, field_name=field,
        previous_value=previous, new_value=new, remarks=remarks,
    )


# ------------------------------------------------------------ release

def release_problems(work_order):
    """Everything that stops a work order being released (empty list = OK)."""
    problems = []
    if work_order.is_released:
        return ["This work order is already released."]
    if not work_order.heat_number.strip():
        problems.append("Enter the raw-material heat number (needed for traceability).")
    problems.extend(materials.material_problems(work_order))
    routing = list(work_order.part.routing.all())
    if not routing:
        problems.append(f"{work_order.part.part_number} has no routing operations.")
        return problems
    finals = [r for r in routing if r.is_final_pdi]
    if len(finals) != 1:
        problems.append("The routing needs exactly one final PDI step.")
    elif routing[-1] is not finals[0]:
        problems.append("The final PDI step must be the last operation.")
    for step in routing:
        if step.is_final_pdi:
            needed = (ChecklistType.PDI, None)
        elif step.requires_first_piece:
            needed = (ChecklistType.FIRST_PIECE, step.op_number)
        else:
            continue
        try:
            get_active_template(work_order.part, *needed)
        except TemplateNotAvailable:
            kind = ChecklistType(needed[0]).label
            problems.append(f"Op {step.op_number} needs a Released {kind} checklist template.")
    return problems


@transaction.atomic
def release_work_order(work_order, user):
    """Release a work order and generate its Digital Job Traveler (PRD 7.1)."""
    _require(user, PERM_RELEASE, "release work orders")
    problems = release_problems(work_order)
    if problems:
        raise ReleaseError(problems)
    traveler = JobTraveler.objects.create(work_order=work_order, quantity=work_order.quantity)
    TravelerOperation.objects.bulk_create(
        TravelerOperation(traveler=traveler, routing_operation=step)
        for step in work_order.part.routing.all()
    )
    work_order.released_by = user
    work_order.released_at = timezone.now()
    work_order.save()
    log_event(
        action=AuditLog.Action.DECISION, user=user, instance=work_order,
        field_name="released_at", new_value=work_order.released_at,
        remarks=f"Released; traveler #{traveler.pk} generated",
    )
    return traveler


# ----------------------------------------------------- scanning helpers

def resolve_traveler_from_barcode(code):
    """Return the JobTraveler for a scanned traveler barcode, or raise InvalidQRToken."""
    traveler_id = read_token(code.strip(), QRKind.TRAVELER)
    try:
        return JobTraveler.objects.get(pk=traveler_id)
    except (JobTraveler.DoesNotExist, ValueError):
        raise InvalidQRToken("This barcode does not match any job traveler.")


# ----------------------------------------------------------- sequence

def ordered_operations(traveler):
    return list(
        traveler.operations.select_related("routing_operation", "run").order_by(
            "routing_operation__op_number"
        )
    )


def next_pending_operation(traveler):
    """The step that must be worked next, or None if all are done or one is running."""
    for op in ordered_operations(traveler):
        if op.status != OP.COMPLETED:
            return op if op.status == OP.PENDING else None
    return None


def blocking_hold(traveler, now=None):
    """(operation, remaining) for the longest hold still running on this traveler, or None."""
    now = now or timezone.now()
    holds = [
        (op, op.hold_until - now)
        for op in traveler.operations.select_related("routing_operation")
        if op.hold_until and op.hold_until > now
    ]
    return max(holds, key=lambda pair: pair[1]) if holds else None


def _assert_material_usable(traveler):
    """PRD 3.3: quarantined or rejected raw material cannot be consumed by production."""
    message = materials.blocking_message(traveler.work_order)
    if message:
        raise OperationBlocked(message)


def _assert_traveler_active(traveler):
    if traveler.status != JobTraveler.Status.ACTIVE:
        raise OperationBlocked(
            f"{traveler} is {traveler.get_status_display()}; no work can continue on it. "
            + ("Material Review Board disposition is required." if traveler.status == "QUARANTINED" else "")
        )
    _assert_material_usable(traveler)


# -------------------------------------------------------------- start

def scan_and_start(operation, operator, machine_token, traveler_code, now=None):
    """Start an operation from the two scans (PRD 5.1): station QR and traveler barcode."""
    machine = resolve_machine_from_qr(machine_token.strip())
    scanned = resolve_traveler_from_barcode(traveler_code)
    if scanned.pk != operation.traveler_id:
        raise OperationBlocked("The scanned traveler barcode belongs to a different job.")
    return start_operation(operation, operator, machine, now)


def start_operation(operation, operator, machine, now=None):
    """Start an operation on a machine (PRD 7.1, 8.2, 2.2, 9.1).

    Blocks when: the traveler is quarantined, a previous step is unfinished,
    a mandatory hold is running, the machine is busy/inactive, or the machine's
    PM / safety checks are overdue. Call outside transaction.atomic so the
    maintenance alert from the PM lock is not rolled back.
    """
    now = now or timezone.now()
    _require(operator, PERM_OPERATE, "start operations")
    traveler = operation.traveler
    step = operation.routing_operation
    _assert_traveler_active(traveler)
    if step.is_final_pdi:
        raise OperationBlocked(
            "The final PDI is completed through the inspection and supervisor approval workflow."
        )
    if operation.status != OP.PENDING:
        raise OperationBlocked(f"{step} is already {operation.get_status_display().lower()}.")

    earlier = [
        op for op in ordered_operations(traveler)
        if op.routing_operation.op_number < step.op_number
    ]
    for op in earlier:
        if op.status != OP.COMPLETED:
            raise OperationBlocked(
                f"{op.routing_operation} must be completed and signed off before {step}."
            )
    for op in earlier:
        if op.hold_until and op.hold_until > now:
            raise HoldActive(
                f"Mandatory Cooling Hold Active ({format_remaining(op.hold_until - now)} remaining) "
                f"after {op.routing_operation}."
            )

    if not machine.is_active:
        raise OperationBlocked(f"{machine.code} is inactive.")
    # A quarantined or closed lot no longer occupies its machine (PRD 6.1).
    if TravelerOperation.objects.filter(
        machine=machine, status=OP.IN_PROGRESS, traveler__status=JobTraveler.Status.ACTIVE
    ).exists():
        raise OperationBlocked(f"{machine.code} is already running another job.")
    assert_machine_ready(machine, now)  # PRD 2.2: raises MachineNotReady and alerts maintenance
    handover.assert_machine_clear(machine, operator, "start an operation", now)  # PRD 12.2

    with transaction.atomic():
        locked = TravelerOperation.objects.select_for_update().get(pk=operation.pk)
        handover.recheck_machine(machine, now)
        if locked.status != OP.PENDING:
            raise OperationBlocked(f"{step} was just started by someone else.")
        locked.status = OP.IN_PROGRESS
        locked.machine = machine
        locked.operator = operator
        locked.shift = shift_for(now)[1]
        locked.clock_in = now
        locked.save()
        run = ProductionRun(traveler_operation=locked)
        if step.requires_first_piece:
            run.state = ProductionRun.State.SETUP  # locked until QA signs off (PRD 9.1)
        else:
            run.state = ProductionRun.State.RUNNING
            _schedule_next_check(run, step, now)
        run.save()
        _audit(operator, locked, "status", OP.PENDING, OP.IN_PROGRESS,
               remarks=f"Started on {machine.code}, shift {locked.get_shift_display()}")
    operation.refresh_from_db()
    return operation


# --------------------------------------------- first piece (PRD 9.1)

def _schedule_next_check(run, step, from_time):
    if step.inspection_interval_minutes:
        run.next_check_due_at = from_time + timedelta(minutes=step.inspection_interval_minutes)
    else:
        run.next_check_due_at = None


@transaction.atomic
def clear_first_piece(run, inspector, now=None):
    """QA inspector signs off the first piece; production counters unlock (PRD 9.1)."""
    now = now or timezone.now()
    _require(inspector, PERM_CLEAR_FIRST_PIECE, "sign off a first piece")
    run = ProductionRun.objects.select_for_update().select_related(
        "traveler_operation__traveler", "traveler_operation__routing_operation"
    ).get(pk=run.pk)
    op = run.traveler_operation
    _assert_traveler_active(op.traveler)
    if op.status != OP.IN_PROGRESS:
        raise OperationBlocked("The operation is not in progress.")
    if run.state != ProductionRun.State.SETUP:
        raise OperationBlocked("The first piece is already signed off.")
    if op.operator_id == inspector.pk:
        raise OperationBlocked("The operator cannot sign off their own first piece.")
    inspection = run.first_piece_inspection  # PRD 9.1 hook: set when QA submits the inspection
    if inspection is None or inspection.status not in (InspectionStatus.SUBMITTED, InspectionStatus.APPROVED):
        raise OperationBlocked(
            "A submitted first-piece inspection is required before the first piece can be signed off."
        )
    if not inspection.all_within_tolerance:
        raise OperationBlocked(
            "The first-piece inspection has out-of-tolerance readings. Correct the setup and inspect again."
        )
    run.state = ProductionRun.State.RUNNING
    run.first_piece_cleared_by = inspector
    run.first_piece_cleared_at = now
    _schedule_next_check(run, op.routing_operation, now)
    run.save()
    _audit(inspector, run, "state", ProductionRun.State.SETUP, ProductionRun.State.RUNNING,
           action=AuditLog.Action.SIGN_OFF, remarks="First-piece sign-off")
    return run


# ---------------------------------------------------- part counter

def _assert_handover_clear(operation, user, action, now):
    """PRD 12.2: nobody works a machine whose previous-shift handover is unacknowledged.

    Runs outside the write transaction so a blocked attempt stays in the audit log.
    """
    machine = TravelerOperation.objects.select_related("machine").get(pk=operation.pk).machine
    handover.assert_machine_clear(machine, user, action, now)


def record_parts(operation, user, count, now=None):
    """Add to the production counter (PRD 9.1: locked until first-piece sign-off)."""
    now = now or timezone.now()
    _require(user, PERM_OPERATE, "record produced parts")
    _assert_handover_clear(operation, user, "record produced parts", now)
    return _record_parts(operation, user, count, now)


@transaction.atomic
def _record_parts(operation, user, count, now):
    _require(user, PERM_OPERATE, "record produced parts")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise OperationBlocked("Enter a whole number of parts, at least 1.")
    operation = TravelerOperation.objects.select_for_update().select_related(
        "traveler", "routing_operation", "run", "machine"
    ).get(pk=operation.pk)
    handover.recheck_machine(operation.machine, now)
    _assert_traveler_active(operation.traveler)
    if operation.status != OP.IN_PROGRESS:
        raise OperationBlocked("The operation is not in progress.")
    run = operation.run
    if run.state != ProductionRun.State.RUNNING:
        raise FirstPieceLocked(
            "Station is in Setup Mode: the QA inspector must sign off the first piece "
            "before production counts can increase."
        )
    new_total = run.parts_produced + count
    if new_total > operation.traveler.quantity:
        raise OperationBlocked(
            f"The lot is {operation.traveler.quantity} parts; {run.parts_produced} are already "
            f"recorded, so {count} more would exceed it."
        )
    previous = run.parts_produced
    run.parts_produced = new_total
    step = operation.routing_operation
    if (
        step.inspection_every_parts
        and run.check_due_since is None
        and new_total - run.parts_at_last_check >= step.inspection_every_parts
    ):
        run.check_due_since = now
    run.save()
    _audit(user, run, "parts_produced", previous, new_total)
    return run


# ------------------------------------- periodic inspections (PRD 9.2)

@dataclass(frozen=True)
class InspectionState:
    due: bool = False
    overdue: bool = False  # grace period used up, job timer paused
    due_at: object = None
    message: str = ""


def inspection_state(run, now=None):
    """Is a periodic inspection due? Calculated on demand, no background worker."""
    now = now or timezone.now()
    step = run.traveler_operation.routing_operation
    if run.state != ProductionRun.State.RUNNING or not step.has_periodic_inspection:
        return InspectionState()
    candidates = []
    if step.inspection_interval_minutes and run.next_check_due_at:
        candidates.append(run.next_check_due_at)
    if run.check_due_since:
        candidates.append(run.check_due_since)
    reached = [c for c in candidates if c <= now]
    if not reached:
        return InspectionState()
    due_at = min(reached)
    if now > due_at + INSPECTION_GRACE:
        return InspectionState(
            True, True, due_at,
            "Periodic Quality Inspection overdue: the job timer is paused until the sample is logged.",
        )
    left = due_at + INSPECTION_GRACE - now
    return InspectionState(
        True, False, due_at,
        f"Periodic Quality Inspection Due ({format_remaining(left)} left before the job timer pauses).",
    )


@transaction.atomic
def log_periodic_check(run, inspector, now=None):
    """Record that the required sample was measured; restarts the interval (PRD 9.2).

    If the 15-minute grace had expired, the overdue time is added to the run's
    paused time so it does not count towards the cycle time.
    """
    now = now or timezone.now()
    _require(inspector, PERM_LOG_CHECK, "log periodic inspections")
    run = ProductionRun.objects.select_for_update().select_related(
        "traveler_operation__traveler", "traveler_operation__routing_operation"
    ).get(pk=run.pk)
    op = run.traveler_operation
    step = op.routing_operation
    _assert_traveler_active(op.traveler)
    if op.status != OP.IN_PROGRESS or run.state != ProductionRun.State.RUNNING:
        raise OperationBlocked("A periodic check can only be logged while the operation is running.")
    if not step.has_periodic_inspection:
        raise OperationBlocked(f"{step} has no periodic inspection configured.")
    state = inspection_state(run, now)
    if state.overdue:
        run.paused_seconds += int((now - (state.due_at + INSPECTION_GRACE)).total_seconds())
    run.last_check_at = now
    run.parts_at_last_check = run.parts_produced
    run.check_due_since = None
    _schedule_next_check(run, step, now)
    run.save()
    _audit(inspector, run, "last_check_at", None, now,
           action=AuditLog.Action.SIGN_OFF,
           remarks=f"Periodic check logged at {run.parts_produced} parts"
                   + (f"; {run.paused_seconds}s timer pause" if state.overdue else ""))
    return run


# ------------------------------------------------------------ complete

def complete_operation(operation, user, now=None):
    """Clock out and sign off an operation (PRD 7.1, 9.2); starts its hold timer (PRD 8.2)."""
    now = now or timezone.now()
    _require(user, PERM_OPERATE, "finish operations")
    _assert_handover_clear(operation, user, "finish an operation", now)
    return _complete_operation(operation, user, now)


@transaction.atomic
def _complete_operation(operation, user, now):
    _require(user, PERM_OPERATE, "finish operations")
    operation = TravelerOperation.objects.select_for_update().select_related(
        "traveler", "routing_operation", "run", "machine"
    ).get(pk=operation.pk)
    handover.recheck_machine(operation.machine, now)
    traveler, step = operation.traveler, operation.routing_operation
    _assert_traveler_active(traveler)
    if operation.status != OP.IN_PROGRESS:
        raise OperationBlocked("The operation is not in progress.")
    run = operation.run  # only exists once the operation has started
    if run.state != ProductionRun.State.RUNNING:
        raise FirstPieceLocked("The first piece has not been signed off, so this step cannot finish.")
    if run.parts_produced != traveler.quantity:
        raise OperationBlocked(
            f"{run.parts_produced} of {traveler.quantity} parts recorded. "
            "Record every part in the lot before signing off."
        )
    state = inspection_state(run, now)
    if state.due:
        raise OperationBlocked(
            "Periodic Quality Inspection Due: the QA inspector must log the sample before this step can finish."
        )

    elapsed = int((now - operation.clock_in).total_seconds())
    operation.status = OP.COMPLETED
    operation.clock_out = now
    operation.cycle_seconds = max(0, elapsed - run.paused_seconds)
    operation.signed_off_by = user
    if step.hold_minutes:
        operation.hold_until = now + timedelta(minutes=step.hold_minutes)
    operation.save()
    _audit(user, operation, "status", OP.IN_PROGRESS, OP.COMPLETED,
           action=AuditLog.Action.SIGN_OFF,
           remarks=f"Signed off {step}" + (f"; hold until {operation.hold_until:%H:%M}" if operation.hold_until else ""))

    if not traveler.operations.exclude(status=OP.COMPLETED).exists():
        traveler.status = JobTraveler.Status.COMPLETED
        traveler.save(update_fields=["status"])
    return operation


# ----------------------------------------------- final PDI (PRD 6.1, 10)

@transaction.atomic
def complete_final_pdi(operation, approver, inspection, now=None):
    """Close the final PDI step once the PDI inspection is approved or conceded.

    Called by the review app (Step 6); the only way this step can be completed.
    Completes the traveler when every step is done and issues the part serials.
    """
    now = now or timezone.now()
    operation = TravelerOperation.objects.select_for_update().select_related(
        "traveler", "routing_operation"
    ).get(pk=operation.pk)
    traveler, step = operation.traveler, operation.routing_operation
    if not step.is_final_pdi:
        raise OperationBlocked("Only the final PDI step is completed by inspection approval.")
    _assert_traveler_active(traveler)
    if operation.status != OP.PENDING:
        raise OperationBlocked(f"{step} is already {operation.get_status_display().lower()}.")
    unfinished = traveler.operations.exclude(pk=operation.pk).exclude(status=OP.COMPLETED)
    if unfinished.exists():
        raise OperationBlocked("Every production step must be completed before the final PDI is closed.")
    operation.status = OP.COMPLETED
    operation.machine = inspection.machine
    operation.operator = inspection.inspector
    operation.shift = shift_for(inspection.started_at)[1]
    operation.clock_in = inspection.started_at
    operation.clock_out = now
    operation.cycle_seconds = max(0, int((now - inspection.started_at).total_seconds()))
    operation.signed_off_by = approver
    operation.save()
    _audit(approver, operation, "status", OP.PENDING, OP.COMPLETED,
           action=AuditLog.Action.SIGN_OFF, remarks=f"Final PDI closed by inspection #{inspection.pk}")
    traveler.status = JobTraveler.Status.COMPLETED
    traveler.save(update_fields=["status"])
    generate_serials(traveler, approver)
    return operation


# --------------------------------------------- serials and traceability

@transaction.atomic
def generate_serials(traveler, user):
    """Create one serial per finished part of a completed traveler (PRD 7.2)."""
    if traveler.status != JobTraveler.Status.COMPLETED:
        raise OperationBlocked("Serial numbers are issued only for a completed traveler.")
    if traveler.serials.exists():
        raise OperationBlocked("Serial numbers were already issued for this traveler.")
    prefix = traveler.work_order.number + (f"-R{traveler.pk}" if traveler.is_rework else "")
    serials = PartSerial.objects.bulk_create(
        PartSerial(traveler=traveler, serial_number=f"{prefix}-{i:04d}")
        for i in range(1, traveler.quantity + 1)
    )
    log_event(
        action=AuditLog.Action.CREATE, user=user, instance=traveler,
        new_value=f"{len(serials)} serials", remarks=f"{prefix}-0001 .. {prefix}-{traveler.quantity:04d}",
    )
    return serials


def forward_trace(heat_number):
    """Recall query: every work order (and its serials) made from a heat lot (PRD 7.2)."""
    return list(
        WorkOrder.objects.filter(heat_number__iexact=heat_number.strip())
        .select_related("part")
        .prefetch_related("travelers__serials")
    )


def backward_trace(serial_number):
    """Birth certificate of one part: heat lot, work order and every operation (PRD 7.2)."""
    try:
        serial = PartSerial.objects.select_related(
            "traveler__work_order__part"
        ).get(serial_number__iexact=serial_number.strip())
    except PartSerial.DoesNotExist:
        return None
    traveler = serial.traveler
    operations = traveler.operations.select_related(
        "routing_operation", "machine", "operator", "signed_off_by"
    )
    return {
        "serial": serial,
        "traveler": traveler,
        "work_order": traveler.work_order,
        "operations": list(operations),
        "lots": materials.lots_for_work_order(traveler.work_order),
    }
