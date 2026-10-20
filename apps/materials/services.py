"""Raw material intake, MTC verification, lab testing, quarantine, SNCR and issue to work orders (PRD 3, 7.2).

Rules live here; views only call these functions. The release and operation rules of
the production app, and the dispatch interlock, ask `blocking_lots` / `material_problems`
instead of keeping their own copy of "is this stock usable".

Call the functions that notify or audit blocked attempts outside any transaction, so the
record of a blocked attempt and the alerts are not rolled back with an error.
"""

import hashlib
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.audit import log_event
from apps.core.exceptions import InvalidQRToken
from apps.core.models import AuditLog
from apps.core.notifications import notify_role
from apps.core.signing import QRKind, read_token
from apps.production.models import WorkOrder

from .exceptions import IntakeRejected, InvalidTransition, MaterialBlocked, MaterialsError, NotAuthorized
from .models import (
    SNCR, IntakeStatus, LabResult, MaterialIssue, MTCReading, RawMaterialIntake, Supplier,
)

PERM_CREATE = "materials.create_intake"
PERM_REJECT = "materials.reject_intake"
PERM_REVIEW_MTC = "materials.review_mtc"
PERM_LAB = "materials.record_lab_result"
PERM_QUARANTINE = "materials.quarantine_lot"
PERM_RESOLVE = "materials.resolve_sncr"
PERM_ISSUE = "materials.issue_material"

MAX_MTC_BYTES = 10 * 1024 * 1024
MIN_DETAIL_CHARS = 10
MIN_RECALL_CHARS = 20
HEAT_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9./-]{1,49}$")
_SIGNATURES = ((b"%PDF-", "pdf"), (b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"))

# PRD 3.3: Purchasing and Quality Management are told about every SNCR.
SNCR_ROLES = (Role.PURCHASING, Role.QUALITY_MANAGER, Role.QUALITY_HEAD)
REVIEW_ROLES = (Role.QUALITY_MANAGER, Role.QUALITY_ENGINEER)

S = IntakeStatus


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


def _audit(user, instance, field, previous, new, action=AuditLog.Action.DECISION, remarks=""):
    log_event(
        action=action, user=user, instance=instance, field_name=field,
        previous_value=previous, new_value=new, remarks=remarks,
    )


def normalise_heat(value):
    return re.sub(r"\s+", "", (value or "")).upper()


def _lock(intake):
    return RawMaterialIntake.objects.select_for_update().select_related("supplier", "grade").get(pk=intake.pk)


def _move(intake, new_status, user, remarks, now):
    previous = intake.status
    intake.status = new_status
    if new_status == S.CLEARED:
        intake.cleared_at = now
    intake.save()
    _audit(user, intake, "status", previous, new_status, remarks=remarks)


# ------------------------------------------------------------------ intake

def _read_mtc(upload):
    if upload is None:
        raise IntakeRejected("An MTC file must be attached; the intake cannot be saved without it.")
    data = upload.read()
    if not data:
        raise IntakeRejected("The MTC file is empty or unreadable. Scan or upload it again.")
    if len(data) > MAX_MTC_BYTES:
        raise IntakeRejected("The MTC file is larger than 10 MB.")
    kind = next((ext for sig, ext in _SIGNATURES if data.startswith(sig)), None)
    if kind is None:
        raise IntakeRejected("The MTC must be a readable PDF, JPEG or PNG file.")
    return data, kind


def create_intake(
    user, *, supplier, po_number, grade, heat_number, mtc_heat_number, quantity, unit="KG", mtc_upload, now=None,
):
    """Log a delivery at the dock (PRD 3.1). Needs a readable MTC and a verified heat number (PRD 3.2)."""
    now = now or timezone.now()
    _require(user, PERM_CREATE, "log raw material intake")
    if not supplier.is_active:
        raise IntakeRejected(f"Supplier {supplier.name} is not active.")
    if not grade.is_active:
        raise IntakeRejected(f"Material grade {grade.code} is not active.")
    po_number = (po_number or "").strip()
    if not po_number:
        raise IntakeRejected("Enter the purchase order number.")
    heat = normalise_heat(heat_number)
    if not HEAT_PATTERN.match(heat):
        raise IntakeRejected("Enter the heat number exactly as printed (letters, digits, - . /).")
    if normalise_heat(mtc_heat_number) != heat:
        raise IntakeRejected(
            "The heat number on the MTC does not match the heat number entered. Check the certificate and the stock."
        )
    try:
        quantity = Decimal(str(quantity))
    except (InvalidOperation, ValueError):
        raise IntakeRejected("Enter the quantity received as a number.") from None
    if not quantity.is_finite() or quantity <= 0:
        raise IntakeRejected("The quantity received must be more than zero.")
    data, kind = _read_mtc(upload=mtc_upload)
    if RawMaterialIntake.objects.filter(supplier=supplier, heat_number=heat, po_number__iexact=po_number).exists():
        raise IntakeRejected(f"Heat {heat} on PO {po_number} from {supplier.name} was already received.")
    with transaction.atomic():
        intake = RawMaterialIntake(
            supplier=supplier, po_number=po_number, grade=grade, heat_number=heat, quantity=quantity,
            unit=unit, lab_required=grade.requires_lab_testing, mtc_sha256=hashlib.sha256(data).hexdigest(),
            received_by=user, received_at=now,
        )
        intake.mtc_file.save(f"{uuid.uuid4().hex}.{kind}", ContentFile(data), save=False)  # never the client's name
        intake.save()
        _audit(user, intake, "status", "", S.RECEIVED, action=AuditLog.Action.CREATE,
               remarks=f"Received heat {heat} on PO {po_number} from {supplier.name}; MTC attached")
    for role in REVIEW_ROLES:
        notify_role(
            role, f"Incoming material {intake.lot_number} awaits MTC review",
            f"{supplier.name} delivered heat {heat} ({grade.code}). Compare the MTC with the grade specification.",
            related=intake,
        )
    return intake


# ------------------------------------------------------- quarantine & SNCR

def _raise_sncr(intake, user, reason_code, details, now):
    """Quarantine the lot and log the SNCR. Caller holds the lock; returns the SNCR."""
    _move(intake, S.SUPPLIER_QUARANTINE, user, f"Supplier quarantine: {details}", now)
    sncr = SNCR.objects.create(intake=intake, reason_code=reason_code, details=details, raised_by=user, raised_at=now)
    _audit(user, sncr, "status", "", SNCR.Status.OPEN, action=AuditLog.Action.CREATE,
           remarks=f"{sncr.number} for {intake.lot_number}: {sncr.get_reason_code_display()}. {details}")
    return sncr


def _notify_sncr(sncr):
    intake = sncr.intake
    affected = affected_work_orders(intake)
    body = (
        f"{sncr.number}: {intake.lot_number}, heat {intake.heat_number} from {intake.supplier.name} "
        f"(PO {intake.po_number}) is in Supplier Quarantine. {sncr.get_reason_code_display()}. {sncr.details}"
    )
    if affected:
        body += " Work orders using this lot: " + ", ".join(w.number for w in affected) + "."
    for role in SNCR_ROLES:
        notify_role(role, f"SNCR {sncr.number}: {intake.lot_number} quarantined", body, related=sncr)
    if affected:
        notify_role(
            Role.PRODUCTION_LEAD, f"Material recall {sncr.number}: stop work on affected orders", body, related=sncr,
        )


def reject_intake(intake, user, reason_code, details, now=None):
    """The dock worker's "Reject Intake" button (PRD 3.3) for stock not yet cleared."""
    now = now or timezone.now()
    _require(user, PERM_REJECT, "reject raw material intake")
    if reason_code not in SNCR.Reason.values or reason_code == SNCR.Reason.RECALL:
        raise IntakeRejected("Select why the intake is rejected.")
    details = (details or "").strip()
    if len(details) < MIN_DETAIL_CHARS:
        raise IntakeRejected(f"Describe the problem in at least {MIN_DETAIL_CHARS} characters.")
    with transaction.atomic():
        locked = _lock(intake)
        if locked.status not in (S.RECEIVED, S.IN_LAB_TESTING):
            raise InvalidTransition(
                f"{locked.lot_number} is {locked.get_status_display().lower()}; only stock that is not yet "
                "cleared can be rejected at intake."
            )
        sncr = _raise_sncr(locked, user, reason_code, details, now)
    _notify_sncr(sncr)
    return sncr


def recall_lot(intake, user, details, now=None):
    """Put an already cleared lot into Supplier Quarantine, e.g. a supplier recall (PRD 7.2)."""
    now = now or timezone.now()
    _require(user, PERM_QUARANTINE, "quarantine a cleared lot")
    details = (details or "").strip()
    if len(details) < MIN_RECALL_CHARS:
        raise IntakeRejected(f"Explain the recall in at least {MIN_RECALL_CHARS} characters.")
    with transaction.atomic():
        locked = _lock(intake)
        if locked.status != S.CLEARED:
            raise InvalidTransition(f"{locked.lot_number} is {locked.get_status_display().lower()}, not cleared stock.")
        sncr = _raise_sncr(locked, user, SNCR.Reason.RECALL, details, now)
    _notify_sncr(sncr)
    return sncr


def resolve_sncr(sncr, user, resolution, reference, now=None):
    """Close the SNCR by returning the stock to the vendor or scrapping it (PRD 3.3, 6.3)."""
    now = now or timezone.now()
    _require(user, PERM_RESOLVE, "close SNCRs")
    if resolution not in SNCR.Resolution.values:
        raise InvalidTransition("Choose return to vendor or scrap.")
    reference = (reference or "").strip()
    if not reference:
        raise InvalidTransition(
            "Enter the debit note reference." if resolution == SNCR.Resolution.RTV else "Enter the scrap cost code."
        )
    with transaction.atomic():
        locked_sncr = SNCR.objects.select_for_update().get(pk=sncr.pk)
        intake = _lock(locked_sncr.intake)
        if locked_sncr.status != SNCR.Status.OPEN or intake.status != S.SUPPLIER_QUARANTINE:
            raise InvalidTransition(f"{locked_sncr.number} is already closed.")
        locked_sncr.status = SNCR.Status.CLOSED
        locked_sncr.resolution = resolution
        locked_sncr.resolution_reference = reference
        locked_sncr.closed_by = user
        locked_sncr.closed_at = now
        locked_sncr.save()
        _move(intake, S.RETURNED if resolution == SNCR.Resolution.RTV else S.SCRAPPED, user,
              f"{locked_sncr.number} closed: {locked_sncr.get_resolution_display()} ({reference})", now)
        _audit(user, locked_sncr, "status", SNCR.Status.OPEN, SNCR.Status.CLOSED,
               remarks=f"{locked_sncr.get_resolution_display()}: {reference}")
    if resolution == SNCR.Resolution.RTV:
        notify_role(
            Role.DOCK_CLERK, f"Return to vendor: {intake.lot_number}",
            f"Send {intake.lot_number} (heat {intake.heat_number}) back to {intake.supplier.name}. "
            f"Debit note request: {reference}.", related=locked_sncr,
        )
    return locked_sncr


# ------------------------------------------------- MTC review and lab test

def review_mtc(intake, user, readings, now=None):
    """QA compares the certified values with the grade specification (PRD 3.2).

    `readings` maps GradeProperty id -> value. Every property of the grade must be
    entered. Any value outside its limit quarantines the lot and raises an SNCR.
    Returns the intake; check `intake.status` for the outcome.
    """
    now = now or timezone.now()
    _require(user, PERM_REVIEW_MTC, "review MTCs")
    sncr = None
    with transaction.atomic():
        locked = _lock(intake)
        if locked.status != S.RECEIVED:
            raise InvalidTransition(f"{locked.lot_number} is {locked.get_status_display().lower()}; its MTC was already reviewed.")
        if locked.received_by_id == user.pk:
            raise InvalidTransition("The person who received the stock cannot also verify its MTC.")
        properties = list(locked.grade.properties.all())
        values, missing = {}, []
        for prop in properties:
            raw = (readings or {}).get(prop.pk)
            if raw is None or str(raw).strip() == "":
                missing.append(prop.name)
                continue
            try:
                value = Decimal(str(raw).strip())
            except InvalidOperation:
                raise IntakeRejected(f"{prop.name}: enter a number.") from None
            if not value.is_finite():
                raise IntakeRejected(f"{prop.name}: enter a number.")
            values[prop.pk] = value
        if missing:
            raise IntakeRejected("Enter the MTC value for: " + ", ".join(missing) + ".")
        failed = []
        for prop in properties:
            reading = MTCReading.objects.create(
                intake=locked, grade_property=prop, value=values[prop.pk], entered_by=user, entered_at=now,
            )
            if not reading.is_within_spec:
                failed.append(f"{prop.name} {reading.value:.4f} (needs {prop.spec_display})")
        locked.mtc_verified_by = user
        locked.mtc_verified_at = now
        if failed:
            sncr = _raise_sncr(locked, user, SNCR.Reason.MTC_OUT_OF_SPEC, "MTC outside specification: " + "; ".join(failed), now)
        elif locked.lab_required:
            _move(locked, S.IN_LAB_TESTING, user, "MTC verified; lab testing required, sample tag issued", now)
        else:
            _move(locked, S.CLEARED, user, "MTC verified against the grade specification", now)
        locked_after = locked
    if sncr:
        _notify_sncr(sncr)
    elif locked_after.status == S.IN_LAB_TESTING:
        for role in REVIEW_ROLES:
            notify_role(
                role, f"Lab sample needed for {locked_after.lot_number}",
                f"{locked_after.lot_number} ({locked_after.grade.code}) needs internal testing. Tag the sample {locked_after.sample_tag}.",
                related=locked_after,
            )
    return locked_after


def record_lab_result(intake, user, *, passed, report_reference, remarks="", now=None):
    """Record the spectro / physical test of a lab-tested lot (PRD 3.2)."""
    now = now or timezone.now()
    _require(user, PERM_LAB, "record lab results")
    report_reference = (report_reference or "").strip()
    remarks = (remarks or "").strip()
    if not report_reference:
        raise IntakeRejected("Enter the lab report reference.")
    if not passed and len(remarks) < MIN_DETAIL_CHARS:
        raise IntakeRejected(f"Describe the failure in at least {MIN_DETAIL_CHARS} characters.")
    sncr = None
    with transaction.atomic():
        locked = _lock(intake)
        if locked.status != S.IN_LAB_TESTING:
            raise InvalidTransition(f"{locked.lot_number} is {locked.get_status_display().lower()}, not in lab testing.")
        LabResult.objects.create(
            intake=locked, passed=bool(passed), report_reference=report_reference, remarks=remarks,
            recorded_by=user, recorded_at=now,
        )
        if passed:
            _move(locked, S.CLEARED, user, f"Lab test passed ({report_reference}); cleared for production", now)
        else:
            sncr = _raise_sncr(locked, user, SNCR.Reason.LAB_FAIL, f"Lab report {report_reference}: {remarks}", now)
    if sncr:
        _notify_sncr(sncr)
    return locked


# ------------------------------------------------------------ work orders

def affected_work_orders(intake):
    return list(WorkOrder.objects.filter(material_issues__intake=intake).distinct().order_by("number"))


def blocking_lots(work_order):
    """Lots issued to this work order that are no longer usable (PRD 3.3). Empty = nothing blocks."""
    return [
        issue.intake
        for issue in work_order.material_issues.select_related("intake__supplier")
        if issue.intake.status != S.CLEARED
    ]


def blocking_message(work_order):
    lots = blocking_lots(work_order)
    if not lots:
        return ""
    return "; ".join(
        f"Raw material lot {lot.lot_number} (heat {lot.heat_number}) is {lot.get_status_display().lower()}"
        for lot in lots
    ) + ". Quarantined or rejected material cannot be used."


def material_problems(work_order):
    """What stops a work order being released because of its raw material (PRD 3.3, 7.2)."""
    heat = work_order.heat_number.strip()
    if not heat:
        return []  # production already asks for the heat number
    issues = list(work_order.material_issues.select_related("intake"))
    if not issues:
        return [f"Issue raw material from a cleared lot with heat number {heat} to this work order before release."]
    problems = []
    for issue in issues:
        lot = issue.intake
        if normalise_heat(lot.heat_number) != normalise_heat(heat):
            problems.append(f"Issued lot {lot.lot_number} has heat number {lot.heat_number}, not {heat}.")
        if lot.status != S.CLEARED:
            problems.append(
                f"Raw material lot {lot.lot_number} is {lot.get_status_display().lower()}; "
                "quarantined or rejected material cannot be used."
            )
    return problems


def issue_material(work_order, intake, quantity, user, now=None):
    """Issue cleared stock to a work order (PRD 3.3, 7.2). Quarantined stock is refused under any circumstances."""
    now = now or timezone.now()
    _require(user, PERM_ISSUE, "issue raw material")
    current = RawMaterialIntake.objects.select_related("supplier").get(pk=intake.pk)
    if current.status != S.CLEARED:
        message = (
            f"{current.lot_number} is {current.get_status_display().lower()} and cannot be issued to production."
        )
        log_event(
            action=AuditLog.Action.OTHER, user=user, instance=current, field_name="issue_blocked",
            new_value=work_order.number, remarks=f"Tried to issue to {work_order.number}. {message}",
        )
        raise MaterialBlocked(message)
    try:
        quantity = Decimal(str(quantity))
    except (InvalidOperation, ValueError):
        raise MaterialsError("Enter the quantity as a number.") from None
    if not quantity.is_finite() or quantity <= 0:
        raise MaterialsError("The quantity issued must be more than zero.")
    with transaction.atomic():
        locked = _lock(intake)
        order = WorkOrder.objects.select_for_update().get(pk=work_order.pk)
        if locked.status != S.CLEARED:
            raise MaterialBlocked(f"{locked.lot_number} is {locked.get_status_display().lower()} and cannot be issued.")
        if order.is_released:
            raise MaterialsError(f"{order.number} is already released; its material cannot change.")
        if normalise_heat(order.heat_number) != normalise_heat(locked.heat_number):
            raise MaterialsError(
                f"{order.number} needs heat {order.heat_number or '(none)'}, but {locked.lot_number} is heat {locked.heat_number}."
            )
        if locked.issues.filter(work_order=order).exists():
            raise MaterialsError(f"{locked.lot_number} was already issued to {order.number}.")
        if quantity > locked.remaining_quantity:
            raise MaterialsError(
                f"Only {locked.remaining_quantity} {locked.get_unit_display()} of {locked.lot_number} remain."
            )
        issue = MaterialIssue.objects.create(
            intake=locked, work_order=order, quantity=quantity, issued_by=user, issued_at=now,
        )
        _audit(user, issue, "quantity", "", quantity, action=AuditLog.Action.CREATE,
               remarks=f"Issued {quantity} {locked.get_unit_display()} of {locked.lot_number} (heat {locked.heat_number}) to {order.number}")
    return issue


# ----------------------------------------------------------------- lookups

def lots_for_heat(heat_number):
    """Every intake of a heat number, for recall queries (PRD 7.2)."""
    heat = normalise_heat(heat_number)
    return list(RawMaterialIntake.objects.filter(heat_number=heat).select_related("supplier", "grade"))


def lots_for_work_order(work_order):
    return [i.intake for i in work_order.material_issues.select_related("intake__supplier", "intake__grade")]


def resolve_intake_from_barcode(code):
    """Return the intake behind a scanned bundle label, or raise MaterialsError (PRD 3.2, 13.1)."""
    try:
        pk = int(read_token((code or "").strip(), QRKind.MATERIAL))
        return RawMaterialIntake.objects.select_related("supplier", "grade").get(pk=pk)
    except (InvalidQRToken, ValueError, RawMaterialIntake.DoesNotExist):
        raise MaterialsError("Unknown or damaged material barcode.") from None


def label_kind(intake):
    """Which label may be printed: sample tag while in lab, bundle barcode once cleared (PRD 3.2)."""
    return {S.IN_LAB_TESTING: "sample", S.CLEARED: "barcode"}.get(intake.status)
