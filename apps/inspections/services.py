"""Inspection business rules (PRD 2.3, 4.3, 5, 9.1, 13.3). Views call these; they hold no HTTP logic.

Reused, not duplicated:
  * tolerance maths           -> checklists.TemplateField.evaluate (inclusive limits)
  * Released-template lookup  -> checklists.services.get_active_template
  * gauge calibration         -> plant.services.get_usable_gauge
  * QR / barcode scans        -> plant.services.resolve_machine_from_qr, production.services.resolve_traveler_from_barcode
  * hold timers, op sequence  -> production.services

Hooks left for later steps:
  * Step 6 (review) adds the supervisor decisions to the Inspection status flow.
  * Periodic (in-process) inspections still use production.services.log_periodic_check.
"""

import hashlib
import uuid
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.checklists.models import ChecklistType, FieldType
from apps.checklists.services import get_active_template
from apps.core.audit import log_event
from apps.core.models import AuditLog
from apps.core.notifications import notify_role
from apps.plant.exceptions import GaugeNotUsable
from apps.plant.services import get_usable_gauge, resolve_machine_from_qr
from apps.production import services as production
from apps.production.models import JobTraveler, ProductionRun, TravelerOperation

from apps.production.exceptions import HoldActive

from .exceptions import (
    CannotStart,
    InspectionFrozen,
    InspectionLocked,
    InvalidMeasurement,
    InvalidPhoto,
    NotAuthorized,
    SubmitBlocked,
)
from .models import DefectPhoto, Inspection, InspectionStatus, Measurement

PERM_CONDUCT = "inspections.conduct_inspection"

MIN_DEFECT_PHOTOS = 2  # PRD 5.4
MAX_DEFECT_PHOTOS = 10
MAX_PHOTO_BYTES = 8 * 1024 * 1024

STARTABLE_TYPES = (ChecklistType.FIRST_PIECE, ChecklistType.PDI)
OP = TravelerOperation.Status


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


def _require_owner(inspection, user):
    if inspection.inspector_id != user.pk:
        raise NotAuthorized("Only the inspector who opened this inspection can work on it.")


def _lock(inspection):
    return Inspection.objects.select_for_update(of=("self",)).select_related(
        "template", "traveler__work_order", "traveler_operation__routing_operation"
    ).get(pk=inspection.pk)


def _assert_open(inspection):
    if inspection.status == InspectionStatus.FROZEN:
        raise InspectionFrozen(
            "This checklist is frozen after a Critical-to-Quality failure. "
            "The Quality Head has been alerted; no more entries are allowed."
        )
    if inspection.status != InspectionStatus.IN_PROGRESS:
        raise InspectionLocked("This inspection has been submitted and can no longer be changed.")


# ------------------------------------------------------------- start (PRD 5.1)

def start_inspection(inspector, machine_token, traveler_code, checklist_type, now=None):
    """Open an inspection from the two scans: station QR plate and traveler barcode.

    No search or pick-list exists: the job is whatever the barcode says, and the
    checklist is the Released template for that lot's current step.
    """
    now = now or timezone.now()
    _require(inspector, PERM_CONDUCT, "run inspections")
    if checklist_type not in STARTABLE_TYPES:
        raise CannotStart("Choose First-Piece or Pre-Dispatch inspection.")
    machine = resolve_machine_from_qr((machine_token or "").strip())
    traveler = production.resolve_traveler_from_barcode(traveler_code or "")
    if traveler.status != JobTraveler.Status.ACTIVE:
        raise CannotStart(f"{traveler} is {traveler.get_status_display()}; it cannot be inspected.")
    if not machine.is_active:
        raise CannotStart(f"{machine.code} is inactive.")
    if Inspection.objects.filter(inspector=inspector, status=InspectionStatus.IN_PROGRESS).exists():
        raise CannotStart("Finish your open inspection before starting another one (PRD 5.1).")

    if checklist_type == ChecklistType.FIRST_PIECE:
        operation = _first_piece_step(traveler, machine)
        template = get_active_template(
            traveler.work_order.part, ChecklistType.FIRST_PIECE, operation.routing_operation.op_number
        )
    else:
        operation = _final_pdi_step(traveler, now)
        template = get_active_template(traveler.work_order.part, ChecklistType.PDI)

    if Inspection.objects.filter(
        traveler_operation=operation, checklist_type=checklist_type, status=InspectionStatus.IN_PROGRESS
    ).exists():
        raise CannotStart("Another inspector already has this checklist open for this job.")

    with transaction.atomic():
        inspection = Inspection.objects.create(
            template=template, checklist_type=checklist_type, traveler=traveler,
            traveler_operation=operation, machine=machine, inspector=inspector, started_at=now,
        )
        log_event(
            action=AuditLog.Action.CREATE, user=inspector, instance=inspection,
            new_value=inspection.get_status_display(),
            remarks=f"Started {inspection.get_checklist_type_display()} at {machine.code}; "
                    f"template {template}; both codes scanned",
        )
    return inspection


def _first_piece_step(traveler, machine):
    setup = [
        op for op in production.ordered_operations(traveler)
        if op.status == OP.IN_PROGRESS and op.run.state == ProductionRun.State.SETUP
    ]
    if not setup:
        raise CannotStart("This job is not waiting for a first-piece inspection.")
    operation = setup[0]
    if operation.machine_id != machine.pk:
        raise CannotStart(
            f"This job is set up on {operation.machine.code}. Scan the QR plate on that machine "
            "(PRD 5.1 physical presence rule)."
        )
    blocking = Inspection.objects.filter(
        traveler_operation=operation, checklist_type=ChecklistType.FIRST_PIECE
    )
    if blocking.filter(status=InspectionStatus.FROZEN).exists():
        raise CannotStart(
            "A first-piece inspection for this step is frozen after a CTQ failure; "
            "wait for the Quality Head's decision."
        )
    if operation.run.first_piece_inspection_id and operation.run.first_piece_inspection.all_within_tolerance:
        raise CannotStart("A passing first-piece inspection is already submitted; sign off the first piece.")
    return operation


def _final_pdi_step(traveler, now):
    operations = production.ordered_operations(traveler)
    final = [op for op in operations if op.routing_operation.is_final_pdi]
    if len(final) != 1:
        raise CannotStart("This traveler has no final PDI step.")
    final = final[0]
    unfinished = [op for op in operations if op is not final and op.status != OP.COMPLETED]
    if final.status != OP.PENDING or unfinished:
        raise CannotStart("Every production step must be completed before the final PDI.")
    hold = production.blocking_hold(traveler, now)
    if hold:
        raise HoldActive(
            f"Mandatory Cooling Hold Active ({production.format_remaining(hold[1])} remaining) "
            f"after {hold[0].routing_operation}."
        )
    if Inspection.objects.filter(
        traveler_operation=final, checklist_type=ChecklistType.PDI,
        status__in=[InspectionStatus.SUBMITTED, InspectionStatus.FROZEN],
    ).exists():
        raise CannotStart("A PDI for this lot is already awaiting a decision.")
    return final


# --------------------------------------------------- readings (PRD 2.3, 5.2, 5.3)

def _parse_value(field, raw):
    if field.field_type == FieldType.VISUAL:
        if not isinstance(raw, bool):
            raise InvalidMeasurement("Answer this visual check with Pass or Fail.")
        return None, raw
    if isinstance(raw, bool) or raw is None or isinstance(raw, float):
        raise InvalidMeasurement("Enter the reading as a number.")
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise InvalidMeasurement("Enter the reading as a number.")
    if not value.is_finite():
        raise InvalidMeasurement("Enter the reading as a number.")
    if value.as_tuple().exponent < -4:
        raise InvalidMeasurement("Enter at most 4 decimal places; rounding could hide a failure.")
    if abs(value) >= Decimal(10) ** 8:
        raise InvalidMeasurement("That reading is too large.")
    return value, None


def _check_gauge(field, gauge_id, on):
    if field.field_type == FieldType.VISUAL:
        return None
    if not (gauge_id or "").strip():
        raise InvalidMeasurement("Scan or enter the gauge serial number before the reading (PRD 2.3).")
    gauge = get_usable_gauge(gauge_id, on)  # raises GaugeNotUsable: unknown / inactive / expired
    if gauge.gauge_type != field.required_gauge_type:
        raise InvalidMeasurement(
            f"{field.label} must be measured with a {field.get_required_gauge_type_display()}; "
            f"{gauge.gauge_id} is a {gauge.get_gauge_type_display()}."
        )
    return gauge


def record_measurement(inspection, field, *, value, gauge_id="", user, now=None):
    """Store one reading, judge it against the template, freeze on a CTQ failure."""
    now = now or timezone.now()
    _require(user, PERM_CONDUCT, "enter measurements")
    inspection = Inspection.objects.select_related("template").get(pk=inspection.pk)
    _require_owner(inspection, user)
    _assert_open(inspection)
    if field.template_id != inspection.template_id:
        raise InvalidMeasurement("That field is not on this inspection's checklist.")
    if inspection.measurements.filter(field=field).exists():
        raise InvalidMeasurement(
            "This reading is already recorded. Recorded readings cannot be changed (PRD 13.3)."
        )
    numeric, boolean = _parse_value(field, value)
    try:
        gauge = _check_gauge(field, gauge_id, timezone.localtime(now).date())
    except GaugeNotUsable as exc:
        # Outside any transaction, so the record of the blocked attempt is kept (PRD 2.3).
        log_event(
            action=AuditLog.Action.OTHER, user=user, instance=inspection, field_name=field.label,
            new_value=gauge_id, remarks=f"Reading blocked: {exc}",
        )
        raise

    with transaction.atomic():
        locked = _lock(inspection)
        _assert_open(locked)
        try:
            with transaction.atomic():
                measurement = Measurement.objects.create(
                    inspection=locked, field=field, value_numeric=numeric, value_bool=boolean,
                    gauge=gauge, entered_by=user, entered_at=now,
                )
        except IntegrityError:
            raise InvalidMeasurement("This reading was just recorded by another request.")
        verdict = "within tolerance" if measurement.is_within_tolerance else "OUT OF TOLERANCE"
        log_event(
            action=AuditLog.Action.CREATE, user=user, instance=measurement, field_name=field.label,
            new_value=measurement.display_value,
            remarks=f"{verdict}; spec {field.spec_display}"
                    + (f"; gauge {gauge.gauge_id}" if gauge else "")
                    + ("; CTQ" if field.is_ctq else ""),
        )
        froze = field.is_ctq and not measurement.is_within_tolerance
        if froze:
            _freeze(locked, measurement, user, now)
    if froze:
        _alert_quality_head(locked, measurement)
    return measurement


def _freeze(inspection, measurement, user, now):
    """PRD 4.3: a CTQ failure freezes the checklist immediately."""
    inspection.status = InspectionStatus.FROZEN
    inspection.frozen_at = now
    inspection.frozen_reason = (
        f"CTQ '{measurement.field.label}' read {measurement.display_value}; "
        f"specification {measurement.field.spec_display}."
    )
    inspection.save()
    log_event(
        action=AuditLog.Action.DECISION, user=user, instance=inspection, field_name="status",
        previous_value=InspectionStatus.IN_PROGRESS, new_value=InspectionStatus.FROZEN,
        remarks=inspection.frozen_reason,
    )


def _alert_quality_head(inspection, measurement):
    notify_role(
        Role.QUALITY_HEAD,
        subject=f"CTQ failure: {inspection.traveler.work_order.number} - {measurement.field.label}",
        body=(
            f"{inspection} was frozen. {inspection.frozen_reason} "
            f"Inspector: {inspection.inspector.get_username()}. "
            "Concession is not allowed for a CTQ failure."
        ),
        related=inspection,
    )


# ---------------------------------------------------- defect photos (PRD 5.4)

_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
)


def _image_kind(data):
    for signature, ext in _SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def add_defect_photo(measurement, upload, user, now=None):
    """Attach a photo to an out-of-tolerance reading (works on a frozen checklist too)."""
    now = now or timezone.now()
    _require(user, PERM_CONDUCT, "upload defect photos")
    measurement = Measurement.objects.select_related("inspection", "field").get(pk=measurement.pk)
    inspection = measurement.inspection
    _require_owner(inspection, user)
    if inspection.status not in (InspectionStatus.IN_PROGRESS, InspectionStatus.FROZEN):
        raise InspectionLocked("This inspection has been submitted; photos can no longer be added.")
    if measurement.is_within_tolerance:
        raise InvalidPhoto("Defect photos are only needed for out-of-tolerance readings.")
    if measurement.photos.count() >= MAX_DEFECT_PHOTOS:
        raise InvalidPhoto(f"At most {MAX_DEFECT_PHOTOS} photos can be added to one reading.")
    if upload is None:
        raise InvalidPhoto("Choose a photo to upload.")
    if getattr(upload, "size", 0) > MAX_PHOTO_BYTES:
        raise InvalidPhoto("The photo is larger than 8 MB.")
    data = upload.read()
    if not data:
        raise InvalidPhoto("The photo file is empty.")
    if len(data) > MAX_PHOTO_BYTES:
        raise InvalidPhoto("The photo is larger than 8 MB.")
    kind = _image_kind(data)
    if kind is None:
        raise InvalidPhoto("Upload a JPEG, PNG or WebP photo.")
    digest = hashlib.sha256(data).hexdigest()
    if measurement.photos.filter(sha256=digest).exists():
        raise InvalidPhoto("This exact photo was already uploaded; take a different picture.")

    with transaction.atomic():
        photo = DefectPhoto.objects.create(
            measurement=measurement, sha256=digest, uploaded_by=user, uploaded_at=now,
            image=ContentFile(data, name=f"{uuid.uuid4().hex}.{kind}"),  # never the client's filename
        )
        log_event(
            action=AuditLog.Action.CREATE, user=user, instance=photo,
            new_value=photo.image.name, remarks=f"Defect photo for {measurement.field.label}",
        )
    return photo


# ---------------------------------------------------------- submit (PRD 5.2, 5.4)

def submission_problems(inspection):
    """Everything that stops the inspection being submitted (empty list = OK)."""
    problems = []
    done = {m.field_id: m for m in inspection.measurements.prefetch_related("photos")}
    missing = [f.label for f in inspection.template.fields.all() if f.pk not in done]
    if missing:
        problems.append("Readings still missing: " + ", ".join(missing) + ".")
    for measurement in done.values():
        if measurement.is_within_tolerance:
            continue
        have = len(measurement.photos.all())
        if have < MIN_DEFECT_PHOTOS:
            problems.append(
                f"{measurement.field.label} is out of tolerance: add {MIN_DEFECT_PHOTOS - have} more "
                f"defect photo(s) ({MIN_DEFECT_PHOTOS} required)."
            )
    return problems


def submit_inspection(inspection, user, remarks="", now=None):
    now = now or timezone.now()
    _require(user, PERM_CONDUCT, "submit inspections")
    with transaction.atomic():
        locked = _lock(inspection)
        _require_owner(locked, user)
        _assert_open(locked)
        problems = submission_problems(locked)
        if problems:
            raise SubmitBlocked(problems)
        locked.status = InspectionStatus.SUBMITTED
        locked.submitted_at = now
        locked.remarks = (remarks or "").strip()
        locked.save()
        passed = locked.all_within_tolerance
        log_event(
            action=AuditLog.Action.SIGN_OFF, user=user, instance=locked, field_name="status",
            previous_value=InspectionStatus.IN_PROGRESS, new_value=InspectionStatus.SUBMITTED,
            remarks=("All readings within tolerance" if passed else "Contains out-of-tolerance readings")
                    + (f"; remarks: {locked.remarks}" if locked.remarks else ""),
        )
        if locked.checklist_type == ChecklistType.FIRST_PIECE:
            _link_first_piece(locked)
    notify_role(
        Role.SHIFT_SUPERVISOR,
        subject=f"Awaiting approval: {locked.traveler.work_order.number} "
                f"{locked.get_checklist_type_display()} (inspection #{locked.pk})",
        body=f"{locked} was submitted by {user.get_username()} and is awaiting approval.",
        related=locked,
    )
    return locked


def _link_first_piece(inspection):
    """Hook for production (PRD 9.1): QA sign-off needs this submitted inspection."""
    run = ProductionRun.objects.select_for_update().get(traveler_operation=inspection.traveler_operation)
    if run.state == ProductionRun.State.SETUP:
        run.first_piece_inspection = inspection
        run.save(update_fields=["first_piece_inspection"])
