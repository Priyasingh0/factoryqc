"""Plant business rules. Views and later apps call these; they hold no HTTP logic."""

from dataclasses import dataclass
from datetime import time, timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.audit import log_event
from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog
from apps.core.notifications import notify_role
from apps.core.signing import QRKind, read_token

from .exceptions import GaugeNotUsable, MachineNotReady, PMCheckError
from .models import Gauge, Machine, MachinePMCheck, Shift


# ---------------------------------------------------------------- shifts

def shift_for(moment=None):
    """Return (shift_date, shift) for a moment in local time.

    Morning 06:00-14:00, Evening 14:00-22:00, Night 22:00-06:00. The early
    hours of a night shift belong to the date the shift started on.
    """
    moment = timezone.localtime(moment or timezone.now())
    t = moment.time()
    if time(6) <= t < time(14):
        return moment.date(), Shift.MORNING
    if time(14) <= t < time(22):
        return moment.date(), Shift.EVENING
    if t >= time(22):
        return moment.date(), Shift.NIGHT
    return moment.date() - timedelta(days=1), Shift.NIGHT


# ----------------------------------------------- PRD 2.2 machine PM lock

@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str = ""


def _readiness(machine, check, shift_date, shift):
    if not machine.is_active:
        return Readiness(False, "Machine is inactive.")
    label = f"{Shift(shift).label} shift on {shift_date:%d %b %Y}"
    if check is None:
        return Readiness(False, f"Daily PM checklist not recorded for the {label}.")
    if not check.completed:
        return Readiness(False, f"Daily PM checklist is not complete for the {label}.")
    if not check.safety_zero_point_ok:
        return Readiness(False, f"Safety zero-point check has not passed for the {label}.")
    return Readiness(True)


def machine_readiness(machine, now=None):
    """Can a job be selected on this machine right now? (PRD 2.2) No side effects."""
    shift_date, shift = shift_for(now)
    check = MachinePMCheck.objects.filter(
        machine=machine, date=shift_date, shift=shift
    ).first()
    return _readiness(machine, check, shift_date, shift)


def readiness_for_machines(machines, now=None):
    """Same rule for many machines with one query. Returns [(machine, Readiness)]."""
    machines = list(machines)
    shift_date, shift = shift_for(now)
    checks = {
        c.machine_id: c
        for c in MachinePMCheck.objects.filter(
            machine__in=machines, date=shift_date, shift=shift
        )
    }
    return [(m, _readiness(m, checks.get(m.pk), shift_date, shift)) for m in machines]


def assert_machine_ready(machine, now=None):
    """Raise MachineNotReady unless the machine may run jobs (PRD 2.2).

    When the block is caused by overdue PM checks, the Maintenance Supervisor is
    alerted (once per machine per shift). Call this BEFORE opening a
    transaction.atomic block, otherwise the alert is rolled back with it.
    """
    readiness = machine_readiness(machine, now)
    if readiness.ready:
        return
    if machine.is_active:
        shift_date, shift = shift_for(now)
        notify_role(
            Role.MAINTENANCE_SUPERVISOR,
            subject=(
                f"PM check overdue: {machine.code} "
                f"({Shift(shift).label} shift {shift_date:%Y-%m-%d})"
            ),
            body=(
                f"{machine} is locked for job selection. {readiness.reason} "
                "Complete the PM checklist and safety zero-point check."
            ),
            related=machine,
        )
    raise MachineNotReady(readiness.reason)


@transaction.atomic
def record_pm_check(machine, user, *, completed, safety_zero_point_ok, remarks="", now=None):
    """Record the PM / safety check for the machine's current shift (PRD 2.2).

    A failed or partial record may be corrected later in the same shift. A
    record that already passed cannot be overwritten. Every change is audited.
    """
    if not machine.is_active:
        raise PMCheckError("Cannot record a PM check for an inactive machine.")
    shift_date, shift = shift_for(now)
    try:
        check = MachinePMCheck.objects.select_for_update().get(
            machine=machine, date=shift_date, shift=shift
        )
    except MachinePMCheck.DoesNotExist:
        check = MachinePMCheck.objects.create(
            machine=machine, date=shift_date, shift=shift, performed_by=user,
            completed=completed, safety_zero_point_ok=safety_zero_point_ok,
            remarks=remarks,
        )
        log_event(
            action=AuditLog.Action.CREATE, user=user, instance=check,
            new_value=f"completed={completed}, safety_zero_point_ok={safety_zero_point_ok}",
            remarks=remarks,
        )
        return check

    if check.is_clear:
        raise PMCheckError("This shift's PM check is already recorded and passed.")

    previous = f"completed={check.completed}, safety_zero_point_ok={check.safety_zero_point_ok}"
    check.completed = completed
    check.safety_zero_point_ok = safety_zero_point_ok
    check.remarks = remarks
    check.performed_by = user
    check.save()
    log_event(
        action=AuditLog.Action.UPDATE, user=user, instance=check,
        previous_value=previous,
        new_value=f"completed={completed}, safety_zero_point_ok={safety_zero_point_ok}",
        remarks=remarks,
    )
    return check


# ------------------------------------------ PRD 2.3 gauge calibration

def assert_gauge_usable(gauge, on=None):
    """Raise GaugeNotUsable if the gauge may not be used for a measurement (PRD 2.3)."""
    on = on or timezone.localdate()
    if not gauge.is_active:
        raise GaugeNotUsable(
            f"Gauge {gauge.gauge_id} is inactive. "
            "Obtain a calibrated tool from the tool crib."
        )
    if not gauge.is_calibrated_on(on):
        raise GaugeNotUsable(
            f"Expired Tool Calibration: gauge {gauge.gauge_id} expired on "
            f"{gauge.calibration_expiry:%d %b %Y}. "
            "Obtain a calibrated tool from the tool crib."
        )


def get_usable_gauge(gauge_id, on=None):
    """Look a gauge up by its serial (typed or scanned) and verify it is usable."""
    try:
        gauge = Gauge.objects.get(gauge_id=gauge_id.strip())
    except Gauge.DoesNotExist:
        raise GaugeNotUsable(f"Unknown gauge '{gauge_id}'. Register it before use.")
    assert_gauge_usable(gauge, on)
    return gauge


# ------------------------------------------------- QR station scanning

def resolve_machine_from_qr(token):
    """Return the Machine for a scanned station QR token, or raise InvalidQRToken."""
    machine_id = read_token(token, QRKind.MACHINE)
    try:
        return Machine.objects.get(pk=machine_id)
    except (Machine.DoesNotExist, ValueError):
        raise InvalidQRToken("This QR code does not match any machine.")
