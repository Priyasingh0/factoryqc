"""Shift handover rules (PRD 12.2) and the end-to-end release picture (PRD 10, 7.2).

Shift handover: the outgoing worker completes the form, a different supervisor
countersigns it, and an incoming worker acknowledges it. Until that happens the
machine is locked for new work in the next shift (start, count, finish).

The release picture reads the state the earlier steps already keep and creates
no flags of its own, so there is no second source of truth to bypass.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.audit import log_event
from apps.core.models import AuditLog
from apps.core.notifications import notify_role
from apps.inspections.models import Inspection, InspectionStatus
from apps.plant.models import Shift
from apps.plant.services import shift_for
from apps.production.models import JobTraveler, ProductionRun, TravelerOperation
from apps.review.models import QuarantineRecord

from .exceptions import HandoverIncomplete, HandoverPending, InvalidTransition, NotAuthorized
from .models import (
    SHIFT_ORDER, HandoverAcknowledgement, HandoverSignoff, MachineHealth, ShiftHandover, ToolWear,
)

PERM_CREATE = "handover.create_shifthandover"
PERM_SIGN = "handover.sign_shifthandover"
PERM_ACK = "handover.acknowledge_shifthandover"

MIN_NOTE = 10  # characters of explanation when something is not normal
OP = TravelerOperation.Status


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


def _audit(user, instance, field_name, previous, new, action, remarks=""):
    log_event(action=action, user=user, instance=instance, field_name=field_name,
              previous_value=previous, new_value=new, remarks=remarks)


# ------------------------------------------------------------ shift timing

_SHIFT_HOURS = {Shift.MORNING: (6, 14), Shift.EVENING: (14, 22), Shift.NIGHT: (22, 6)}


def shift_window(shift_date, shift):
    """Start and end of a shift as aware datetimes in the factory's local time."""
    start_hour, end_hour = _SHIFT_HOURS[shift]
    tz = timezone.get_current_timezone()
    start = datetime.combine(shift_date, time(start_hour), tzinfo=tz)
    end_day = shift_date + timedelta(days=1) if shift == Shift.NIGHT else shift_date
    return start, datetime.combine(end_day, time(end_hour), tzinfo=tz)


def _key(now):
    shift_date, shift = shift_for(now)
    return shift_date, SHIFT_ORDER[shift]


# ------------------------------------------------- what the system knows

def machine_snapshot(machine, now=None):
    """Job status, parts, open quality alerts and pending reviews for a machine this shift (PRD 12.2)."""
    now = now or timezone.now()
    shift_date, shift = shift_for(now)
    start, end = shift_window(shift_date, shift)
    ops = list(
        TravelerOperation.objects.filter(machine=machine, clock_in__lt=end)
        .exclude(clock_out__lt=start)
        .select_related("traveler__work_order", "routing_operation")
    )
    runs = {r.traveler_operation_id: r for r in ProductionRun.objects.filter(traveler_operation__in=ops)}
    open_jobs, parts = [], 0
    for op in ops:
        run = runs.get(op.pk)
        produced = run.parts_produced if run else 0
        parts += produced
        if op.status == OP.IN_PROGRESS and op.traveler.status == JobTraveler.Status.ACTIVE:
            open_jobs.append({
                "work_order": op.traveler.work_order.number,
                "traveler": op.traveler_id,
                "operation": str(op.routing_operation),
                "parts_produced": produced,
                "lot_quantity": op.traveler.quantity,
                "run_state": run.get_state_display() if run else "",
            })
    travelers = {op.traveler_id for op in ops}
    alerts = QuarantineRecord.objects.filter(
        traveler_id__in=travelers, status=QuarantineRecord.Status.OPEN
    ).count() + Inspection.objects.filter(
        traveler_id__in=travelers, status=InspectionStatus.FROZEN
    ).count()
    pending = Inspection.objects.filter(
        traveler_id__in=travelers, status=InspectionStatus.SUBMITTED
    ).count()
    return {"open_jobs": open_jobs, "parts_produced": parts, "open_quality_alerts": alerts,
            "pending_pdi_reviews": pending}


# ---------------------------------------------------------- the form

def _clean_note(text):
    return (text or "").strip()


def handover_problems(snapshot, *, scrap_count, tool_wear, tool_wear_notes, machine_health,
                      machine_health_notes, job_status_notes):
    """Everything wrong with the outgoing form, so the tablet can show it all at once (PRD 12.2)."""
    problems = []
    if not isinstance(scrap_count, int) or isinstance(scrap_count, bool) or scrap_count < 0:
        problems.append("Enter the scrap count as a whole number (0 if none).")
    if tool_wear not in ToolWear.values:
        problems.append("Select the tool wear status.")
    elif tool_wear != ToolWear.OK and len(_clean_note(tool_wear_notes)) < MIN_NOTE:
        problems.append(f"Describe the tool wear (at least {MIN_NOTE} characters).")
    if machine_health not in MachineHealth.values:
        problems.append("Select the machine health.")
    elif machine_health != MachineHealth.GOOD and len(_clean_note(machine_health_notes)) < MIN_NOTE:
        problems.append(f"Describe the machine problem (at least {MIN_NOTE} characters).")
    if snapshot["open_jobs"] and len(_clean_note(job_status_notes)) < MIN_NOTE:
        problems.append(
            f"A job is still running on this machine: describe its status for the next shift "
            f"(at least {MIN_NOTE} characters)."
        )
    return problems


def create_handover(machine, user, *, scrap_count, tool_wear, machine_health, tool_wear_notes="",
                    machine_health_notes="", job_status_notes="", now=None):
    """Save the outgoing shift's handover form for a machine (PRD 12.2)."""
    now = now or timezone.now()
    _require(user, PERM_CREATE, "complete a shift handover")
    if not machine.is_active:
        raise HandoverIncomplete(f"{machine.code} is inactive.")
    with transaction.atomic():
        from apps.plant.models import Machine

        Machine.objects.select_for_update().get(pk=machine.pk)  # one handover form at a time per machine
        snapshot = machine_snapshot(machine, now)
        problems = handover_problems(
            snapshot, scrap_count=scrap_count, tool_wear=tool_wear, tool_wear_notes=tool_wear_notes,
            machine_health=machine_health, machine_health_notes=machine_health_notes,
            job_status_notes=job_status_notes,
        )
        if problems:
            raise HandoverIncomplete(" ".join(problems))
        shift_date, shift = shift_for(now)
        if ShiftHandover.objects.filter(machine=machine, shift_date=shift_date, shift=shift).exists():
            raise InvalidTransition(
                f"A handover for {machine.code} is already recorded for this {Shift(shift).label.lower()} shift."
            )
        try:
            with transaction.atomic():
                handover = ShiftHandover.objects.create(
                    machine=machine, shift_date=shift_date, shift=shift, outgoing_by=user, created_at=now,
                    scrap_count=scrap_count, tool_wear=tool_wear, tool_wear_notes=_clean_note(tool_wear_notes),
                    machine_health=machine_health, machine_health_notes=_clean_note(machine_health_notes),
                    job_status_notes=_clean_note(job_status_notes), **snapshot,
                )
        except IntegrityError:
            raise InvalidTransition("A handover for this machine and shift was just recorded by someone else.")
        _audit(user, handover, "status", "", "Handover recorded", AuditLog.Action.CREATE,
               f"{machine.code}, {handover.get_shift_display()} shift: {snapshot['parts_produced']} parts, "
               f"scrap {scrap_count}, tool wear {tool_wear}, machine {machine_health}, "
               f"{snapshot['open_quality_alerts']} open quality alerts, {snapshot['pending_pdi_reviews']} reviews pending.")
    label = f"{machine.code} ({handover.get_shift_display()} {shift_date:%d %b})"
    notify_role(
        Role.SHIFT_SUPERVISOR, f"Handover needs supervisor sign-off: {label}",
        f"{user.get_username()} completed the shift handover for {machine}. A supervisor must countersign it.",
        related=handover,
    )
    if machine_health != MachineHealth.GOOD or tool_wear == ToolWear.CHANGE_NOW:
        notify_role(
            Role.MAINTENANCE_SUPERVISOR, f"Machine attention needed at handover: {label}",
            f"Machine health: {handover.get_machine_health_display()}. Tooling: {handover.get_tool_wear_display()}. "
            f"{handover.machine_health_notes} {handover.tool_wear_notes}".strip(),
            related=handover,
        )
    return handover


def sign_handover(handover, supervisor, remarks="", now=None):
    """The outgoing supervisor countersigns (PRD 12.2). Not the person who filled in the form."""
    now = now or timezone.now()
    _require(supervisor, PERM_SIGN, "countersign a shift handover")
    with transaction.atomic():
        locked = ShiftHandover.objects.select_for_update().select_related("machine").get(pk=handover.pk)
        if hasattr(locked, "signoff"):
            raise InvalidTransition("This handover is already signed off.")
        if locked.outgoing_by_id == supervisor.pk:
            raise InvalidTransition("You completed this handover, so another supervisor must countersign it.")
        HandoverSignoff.objects.create(
            handover=locked, signed_by=supervisor, signed_at=now, remarks=_clean_note(remarks)
        )
        _audit(supervisor, locked, "status", "Awaiting supervisor sign-off", "Awaiting incoming acknowledgement",
               AuditLog.Action.SIGN_OFF, f"Countersigned handover of {locked.machine.code}")
    machine = locked.machine
    notify_role(
        Role.MACHINE_OPERATOR,
        f"Handover ready to acknowledge: {machine.code} ({locked.get_shift_display()} {locked.shift_date:%d %b})",
        f"The shift handover for {machine} is signed off. Review it and acknowledge before starting work.",
        related=locked,
    )
    return locked


def acknowledge_handover(handover, user, remarks="", now=None):
    """The incoming worker confirms they reviewed the log (PRD 12.2)."""
    now = now or timezone.now()
    _require(user, PERM_ACK, "acknowledge a shift handover")
    try:
        _check_acknowledgeable(handover, user)
    except InvalidTransition as exc:
        log_event(action=AuditLog.Action.OTHER, user=user, instance=handover, field_name="acknowledge",
                  new_value="BLOCKED", remarks=str(exc))
        raise
    with transaction.atomic():
        locked = ShiftHandover.objects.select_for_update().select_related("machine").get(pk=handover.pk)
        _check_acknowledgeable(locked, user)
        HandoverAcknowledgement.objects.create(
            handover=locked, acknowledged_by=user, acknowledged_at=now, remarks=_clean_note(remarks)
        )
        _audit(user, locked, "status", "Awaiting incoming acknowledgement", "Acknowledged",
               AuditLog.Action.SIGN_OFF, f"Acknowledged handover of {locked.machine.code}")
    return locked


def _check_acknowledgeable(handover, user):
    fresh = ShiftHandover.objects.get(pk=handover.pk)
    if hasattr(fresh, "acknowledgement"):
        raise InvalidTransition("This handover has already been acknowledged.")
    if not hasattr(fresh, "signoff"):
        raise InvalidTransition("The outgoing supervisor has not countersigned this handover yet.")
    if user.pk in (fresh.outgoing_by_id, fresh.signoff.signed_by_id):
        raise InvalidTransition("The incoming worker must be someone other than the people who handed over.")


# ----------------------------------------------- the machine lock (PRD 12.2)

def machine_problem(machine, now=None):
    """Why this machine cannot be worked on, or "" when no handover is outstanding."""
    if machine is None:
        return ""
    now = now or timezone.now()
    current = _key(now)
    latest = None
    for handover in ShiftHandover.objects.filter(machine=machine, shift_date__lte=current[0]).select_related(
        "outgoing_by"
    ):
        if handover.shift_key < current and (latest is None or handover.shift_key > latest.shift_key):
            latest = handover
    if latest is None or hasattr(latest, "acknowledgement"):
        return ""
    when = f"{latest.get_shift_display()} shift of {latest.shift_date:%d %b %Y}"
    if not hasattr(latest, "signoff"):
        return (f"Shift handover pending: the {when} handover for {machine.code} has not been countersigned "
                "by a supervisor, so the incoming worker cannot acknowledge it yet.")
    return (f"Shift handover pending: review and acknowledge the {when} handover for {machine.code} "
            "before starting work.")


def missing_handover(machine, now=None):
    """Why the logbook is incomplete: a job was left running across a shift change without a handover (PRD 12.2).

    Shown on the board so supervisors can chase it; it does not lock the machine, because the form
    belongs to the shift that ended and only records of work already done can be locked safely.
    """
    now = now or timezone.now()
    shift_date, shift = shift_for(now)
    start, _ = shift_window(shift_date, shift)
    carried = TravelerOperation.objects.filter(
        machine=machine, status=OP.IN_PROGRESS, clock_in__lt=start, traveler__status=JobTraveler.Status.ACTIVE
    ).exists()
    if not carried:
        return ""
    previous_date, previous = shift_for(start - timedelta(seconds=1))
    if ShiftHandover.objects.filter(machine=machine, shift_date=previous_date, shift=previous).exists():
        return ""
    return (f"No handover was recorded for the {Shift(previous).label} shift of {previous_date:%d %b %Y}, "
            f"but a job was left running on {machine.code}.")


def assert_machine_clear(machine, user, action, now=None):
    """Raise HandoverPending (and audit and alert) while a previous shift's handover is not acknowledged.

    Call outside any transaction so the record of a blocked attempt is kept.
    """
    reason = machine_problem(machine, now)
    if not reason:
        return
    log_event(action=AuditLog.Action.OTHER, user=user, instance=machine, field_name="shift_handover",
              new_value="BLOCKED", remarks=f"Tried to {action}. {reason}")
    notify_role(
        Role.SHIFT_SUPERVISOR, f"Work blocked by pending handover: {machine.code}", reason, related=machine
    )
    raise HandoverPending(reason)


def recheck_machine(machine, now=None):
    """Same lock inside the write transaction (no audit: the caller already recorded the attempt)."""
    reason = machine_problem(machine, now)
    if reason:
        raise HandoverPending(reason)


def pending_handovers(machines=None, now=None):
    """Handovers not yet acknowledged, oldest first, for the board."""
    qs = ShiftHandover.objects.select_related("machine", "outgoing_by").filter(acknowledgement__isnull=True)
    if machines is not None:
        qs = qs.filter(machine__in=machines)
    return list(qs.order_by("shift_date", "created_at"))


# ------------------------------------------- end-to-end release picture

@dataclass
class Stage:
    code: str
    title: str
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems


@dataclass
class Flow:
    traveler: object
    stages: list

    @property
    def ready(self):
        return all(s.ok for s in self.stages)

    @property
    def problems(self):
        return [p for s in self.stages for p in s.problems]

    @property
    def current(self):
        return next((s for s in self.stages if not s.ok), None)


def flow_status(traveler, now=None):
    """Material, production, quality and dispatch stages for a traveler, each with its exact blockers.

    Composed from the existing rules (materials.blocking_message, the dispatch interlock,
    production.blocking_hold); nothing here can pass a lot those rules would stop.
    """
    from apps.dispatch import services as dispatch
    from apps.materials import services as materials
    from apps.production import services as production

    now = now or timezone.now()
    traveler = JobTraveler.objects.select_related("work_order__part").get(pk=traveler.pk)
    clearance = dispatch.clearance(traveler, now)
    by_code = {c.code: c for c in clearance.checks}

    material = Stage("MATERIAL", "Raw material cleared and issued")
    message = materials.blocking_message(traveler.work_order)
    if message:
        material.problems.append(message)

    # The dispatch check lists every step; the final PDI step belongs to the Final PDI stage below.
    pdi_step = dispatch.final_pdi_operation(traveler)
    pdi_prefix = f"Op {pdi_step.routing_operation.op_number} " if pdi_step else None
    production_stage = Stage("PRODUCTION", "All manufacturing operations signed off")
    pdi_op_problems = []
    for problem in by_code["OPERATIONS"].problems:
        (pdi_op_problems if pdi_prefix and problem.startswith(pdi_prefix) else production_stage.problems).append(problem)

    quality = Stage("QUALITY", "No quality hold and holds fulfilled")
    quality.problems += by_code["HOLDS"].problems
    pdi = Stage("FINAL_PDI", "Final PDI signed and approved by Quality")
    pdi.problems += pdi_op_problems + by_code["FINAL_PDI"].problems

    dispatch_stage = Stage("DISPATCH", "Eligible for gate pass, packing slip, invoice and labels")
    earlier = [material, production_stage, quality, pdi]
    if any(not s.ok for s in earlier) or not clearance.cleared:
        dispatch_stage.problems.append("Dispatch stays locked until every stage above is complete.")
    return Flow(traveler, earlier + [dispatch_stage])
