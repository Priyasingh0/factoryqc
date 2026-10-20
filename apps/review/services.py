"""Supervisor review, concessions, quarantine and MRB disposition (PRD 4.3, 6, 9.1, 10).

Integrates, never duplicates:
  * inspection statuses and immutability -> inspections.models (the only legal moves are in ALLOWED_TRANSITIONS)
  * final PDI step + traveler completion + serials -> production.services.complete_final_pdi
  * quarantine lock -> JobTraveler.status, which production.services already refuses to work on
Hooks left for later steps:
  * Step 7 (dispatch) reads the PDI decision and quarantine state for its shipping interlock.
  * Step 8 (materials) will take scrapped / returned stock out of inventory.
"""

import uuid
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.checklists.models import ChecklistType
from apps.core.audit import log_event
from apps.core.models import AuditLog
from apps.core.notifications import notify_role
from apps.inspections.models import Inspection, InspectionStatus
from apps.production import services as production
from apps.production.models import JobTraveler, TravelerOperation

from .exceptions import (
    CtqConcessionBlocked,
    DecisionBlocked,
    InvalidDisposition,
    NotAuthorized,
    SignoffError,
)
from .models import (
    MRB_ROLES,
    Concession,
    Decision,
    Disposition,
    MRBDisposition,
    MRBSignoff,
    QuarantineRecord,
    ReasonCode,
    SupervisorDecision,
)

PERM_APPROVE = "review.approve_inspection"
PERM_CONCESSION = "review.issue_concession"
PERM_QUARANTINE = "review.quarantine_lot"
PERM_PROPOSE = "review.propose_disposition"
PERM_SIGN = "review.sign_disposition"

MIN_JUSTIFICATION_CHARS = 20
MAX_APPROVAL_BYTES = 10 * 1024 * 1024
OP = TravelerOperation.Status
DECIDABLE = (InspectionStatus.SUBMITTED, InspectionStatus.FROZEN)


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


def _audit(user, instance, field, previous, new, action=AuditLog.Action.DECISION, remarks=""):
    log_event(
        action=action, user=user, instance=instance, field_name=field,
        previous_value=previous, new_value=new, remarks=remarks,
    )


def _lock(inspection):
    return Inspection.objects.select_for_update(of=("self",)).select_related(
        "template", "traveler__work_order", "traveler_operation__routing_operation", "inspector"
    ).get(pk=inspection.pk)


def _not_the_inspector(inspection, user):
    if inspection.inspector_id == user.pk:
        raise NotAuthorized("You cannot decide on an inspection you carried out yourself.")


# ------------------------------------------------- what may be decided

def _failed_readings(inspection):
    return list(inspection.measurements.filter(is_within_tolerance=False).select_related("field"))


def _check_open_for_decision(inspection):
    if inspection.status == InspectionStatus.SUBMITTED:
        return
    if inspection.status == InspectionStatus.FROZEN:
        raise DecisionBlocked("This checklist is frozen after a CTQ failure; it can only be quarantined.")
    if inspection.status == InspectionStatus.IN_PROGRESS:
        raise DecisionBlocked("The inspector has not submitted this inspection yet.")
    raise DecisionBlocked(f"This inspection was already decided ({inspection.get_status_display()}).")


def _check_approvable(inspection):
    _check_open_for_decision(inspection)
    if _failed_readings(inspection):
        raise DecisionBlocked(
            "Approval needs every reading within tolerance. Use a concession (non-CTQ only) or quarantine."
        )


def _check_concession_allowed(inspection):
    if inspection.status == InspectionStatus.FROZEN:
        raise CtqConcessionBlocked(
            "A concession is not allowed: this checklist was frozen by a Critical-to-Quality failure (PRD 4.3)."
        )
    _check_open_for_decision(inspection)
    if inspection.checklist_type == ChecklistType.FIRST_PIECE:
        raise DecisionBlocked(
            "A first piece cannot be released on concession; correct the setup and inspect again."
        )
    failed = _failed_readings(inspection)
    if not failed:
        raise DecisionBlocked("Every reading is within tolerance; approve the inspection instead.")
    ctq = [m.field.label for m in failed if m.field.is_ctq]
    if ctq:
        raise CtqConcessionBlocked(
            "A concession is not allowed for a Critical-to-Quality failure: " + ", ".join(ctq) + "."
        )


def _check_quarantinable(inspection):
    if inspection.status not in DECIDABLE:
        _check_open_for_decision(inspection)
    if inspection.traveler.status != JobTraveler.Status.ACTIVE:
        raise DecisionBlocked(f"{inspection.traveler} is already {inspection.traveler.get_status_display()}.")


def decision_problems(inspection):
    """Why each decision is unavailable right now (empty string = available). For the review page."""
    out = {}
    for name, check in (
        ("approve", _check_approvable),
        ("concession", _check_concession_allowed),
        ("quarantine", _check_quarantinable),
    ):
        try:
            check(inspection)
            out[name] = ""
        except DecisionBlocked as exc:
            out[name] = str(exc)
    return out


# --------------------------------------------------------- the decisions

def approve_inspection(inspection, user, remarks="", now=None):
    now = now or timezone.now()
    _require(user, PERM_APPROVE, "approve inspections")
    with transaction.atomic():
        locked = _lock(inspection)
        _not_the_inspector(locked, user)
        _check_approvable(locked)
        decision = _record_decision(locked, Decision.APPROVED, InspectionStatus.APPROVED, user, remarks, now)
        _close_final_pdi_if_needed(locked, user, now)
    return decision


def issue_concession(inspection, user, reason_code, justification, now=None):
    now = now or timezone.now()
    _require(user, PERM_CONCESSION, "issue concessions")
    if not user.may_issue_concession:
        raise NotAuthorized(
            "Only the Quality Manager or a Shift Supervisor authorised for concessions can issue one (PRD 6.1)."
        )
    if reason_code not in ReasonCode.values:
        raise DecisionBlocked("Select a concession reason code.")
    justification = (justification or "").strip()
    if len(justification) < MIN_JUSTIFICATION_CHARS:
        raise DecisionBlocked(
            f"Write the engineering justification in detail (at least {MIN_JUSTIFICATION_CHARS} characters)."
        )
    with transaction.atomic():
        locked = _lock(inspection)
        _not_the_inspector(locked, user)
        _check_concession_allowed(locked)
        failed = ", ".join(m.field.label for m in _failed_readings(locked))
        decision = _record_decision(
            locked, Decision.CONCESSION, InspectionStatus.CONCESSION, user,
            f"Concession for: {failed}", now,
        )
        concession = Concession.objects.create(
            decision=decision, reason_code=reason_code, justification=justification,
            issued_by=user, issued_at=now,
        )
        _audit(user, concession, "justification", "", justification,
               remarks=f"Concession {concession.get_reason_code_display()} on inspection #{locked.pk}; "
                       "lot permanently flagged in quality history")
        _close_final_pdi_if_needed(locked, user, now)
    notify_role(
        Role.QUALITY_HEAD,
        subject=f"Concession issued: {locked.traveler.work_order.number} (inspection #{locked.pk})",
        body=f"{user.get_username()} released the lot on concession ({concession.get_reason_code_display()}). "
             f"Out of tolerance: {failed}. {justification}",
        related=locked,
    )
    return decision


def quarantine_lot(inspection, user, reason, now=None):
    now = now or timezone.now()
    _require(user, PERM_QUARANTINE, "quarantine lots")
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DecisionBlocked("Explain why the lot is rejected (at least 10 characters).")
    with transaction.atomic():
        locked = _lock(inspection)
        _not_the_inspector(locked, user)
        _check_quarantinable(locked)
        previous = locked.status
        decision = _record_decision(locked, Decision.QUARANTINED, InspectionStatus.QUARANTINED, user, reason, now)
        traveler = locked.traveler
        record = QuarantineRecord.objects.create(
            traveler=traveler, inspection=locked, reason=reason, created_by=user, created_at=now,
        )
        traveler.status = JobTraveler.Status.QUARANTINED
        traveler.save(update_fields=["status"])  # production refuses all further work on it
        _audit(user, traveler, "status", JobTraveler.Status.ACTIVE, JobTraveler.Status.QUARANTINED,
               remarks=f"Moved to {record.location}; {reason}")
    for role in MRB_ROLES:
        notify_role(
            role,
            subject=f"Lot quarantined: {traveler.work_order.number} (MRB disposition needed)",
            body=f"{traveler} was moved to the {record.location} after inspection #{locked.pk} "
                 f"({previous}). {reason}",
            related=record,
        )
    return record


def _record_decision(inspection, decision, new_status, user, remarks, now):
    previous = inspection.status
    row = SupervisorDecision.objects.create(
        inspection=inspection, decision=decision, decided_by=user, decided_at=now, remarks=remarks,
    )
    inspection.status = new_status
    inspection.save()  # legal move checked by Inspection.save()
    _audit(user, inspection, "status", previous, new_status,
           remarks=f"{row.get_decision_display()}" + (f": {remarks}" if remarks else ""))
    return row


def _final_pdi_operation(traveler):
    return traveler.operations.filter(routing_operation__is_final_pdi=True).first()


def _close_final_pdi_if_needed(inspection, user, now):
    """PRD 10: an approved or conceded PDI closes the final step and completes the lot."""
    if inspection.checklist_type == ChecklistType.PDI:
        production.complete_final_pdi(_final_pdi_operation(inspection.traveler), user, inspection, now)


# ------------------------------------------------------- MRB (PRD 6.3)

def _disposition_details(quarantine, disposition, notes, rework_from_op, scrap_cost_code,
                         scrap_bin_verified, debit_note_ref):
    if disposition not in Disposition.values:
        raise InvalidDisposition("Choose a disposition.")
    if not (notes or "").strip():
        raise InvalidDisposition("Describe the corrective action or reason.")
    details = dict(rework_from_op=None, scrap_cost_code="", scrap_bin_verified=False, debit_note_ref="")
    if disposition == Disposition.REWORK:
        routing = list(quarantine.traveler.work_order.part.routing.all())
        allowed = [r.op_number for r in routing if not r.is_final_pdi]
        if rework_from_op not in allowed:
            raise InvalidDisposition(
                "Choose the operation number where rework restarts (one of: "
                + ", ".join(str(n) for n in allowed) + ")."
            )
        details["rework_from_op"] = rework_from_op
    elif disposition == Disposition.SCRAP:
        if not (scrap_cost_code or "").strip():
            raise InvalidDisposition("A scrap cost code is required.")
        if not scrap_bin_verified:
            raise InvalidDisposition("Physical verification at the scrap bin station must be confirmed.")
        details.update(scrap_cost_code=scrap_cost_code.strip(), scrap_bin_verified=True)
    elif disposition == Disposition.RTV:
        if not (debit_note_ref or "").strip():
            raise InvalidDisposition("A debit note reference is required for return to vendor.")
        details["debit_note_ref"] = debit_note_ref.strip()
    return details


def propose_disposition(quarantine, user, disposition, notes, *, rework_from_op=None, scrap_cost_code="",
                        scrap_bin_verified=False, debit_note_ref="", now=None):
    now = now or timezone.now()
    _require(user, PERM_PROPOSE, "propose an MRB disposition")
    with transaction.atomic():
        quarantine = QuarantineRecord.objects.select_for_update().select_related(
            "traveler__work_order__part"
        ).get(pk=quarantine.pk)
        if quarantine.status != QuarantineRecord.Status.OPEN:
            raise InvalidDisposition("This quarantine is already closed.")
        if MRBDisposition.objects.filter(quarantine=quarantine).exists():
            raise InvalidDisposition("A disposition has already been proposed for this lot.")
        details = _disposition_details(
            quarantine, disposition, notes, rework_from_op, scrap_cost_code, scrap_bin_verified, debit_note_ref
        )
        row = MRBDisposition.objects.create(
            quarantine=quarantine, disposition=disposition, notes=notes.strip(),
            proposed_by=user, proposed_at=now, **details,
        )
        _audit(user, row, "disposition", "", disposition, remarks=notes.strip())
    for role in MRB_ROLES:
        notify_role(
            role,
            subject=f"MRB sign-off needed: {quarantine.traveler.work_order.number} - {row.get_disposition_display()}",
            body=f"{user.get_username()} proposed '{row.get_disposition_display()}' for {quarantine.traveler}. "
                 f"{row.notes}",
            related=row,
        )
    return row


def sign_disposition(disposition, user, now=None):
    now = now or timezone.now()
    _require(user, PERM_SIGN, "sign off MRB dispositions")
    if user.role not in MRB_ROLES:
        raise SignoffError("Only the Plant Manager, Quality Head and Production Lead sit on the MRB.")
    with transaction.atomic():
        disposition = MRBDisposition.objects.select_for_update().select_related(
            "quarantine__traveler__work_order__part", "quarantine__inspection"
        ).get(pk=disposition.pk)
        if disposition.is_executed:
            raise SignoffError("This disposition has already been executed.")
        if disposition.signoffs.filter(role=user.role).exists():
            raise SignoffError(f"The {user.get_role_display()} has already signed this disposition.")
        if disposition.signoffs.filter(user=user).exists():
            raise SignoffError("You have already signed this disposition.")
        signoff = MRBSignoff.objects.create(disposition=disposition, user=user, role=user.role, signed_at=now)
        _audit(user, signoff, "role", "", user.role, action=AuditLog.Action.SIGN_OFF,
               remarks=f"MRB sign-off on {disposition.get_disposition_display()}")
        executed = _try_execute(disposition, user, now)
    return executed


_SIGNATURES = ((b"%PDF-", "pdf"), (b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"))


def attach_customer_approval(disposition, upload, user, now=None):
    """Customer Special Concession: the batch stays held until the written approval is uploaded."""
    now = now or timezone.now()
    _require(user, PERM_PROPOSE, "upload the customer approval")
    if upload is None:
        raise InvalidDisposition("Choose the customer's written approval to upload.")
    data = upload.read()
    if not data or len(data) > MAX_APPROVAL_BYTES:
        raise InvalidDisposition("The approval must be a non-empty file of at most 10 MB.")
    kind = next((ext for sig, ext in _SIGNATURES if data.startswith(sig)), None)
    if kind is None:
        raise InvalidDisposition("Upload the approval as a PDF, JPEG or PNG file.")
    with transaction.atomic():
        disposition = MRBDisposition.objects.select_for_update().select_related(
            "quarantine__traveler__work_order__part", "quarantine__inspection"
        ).get(pk=disposition.pk)
        if disposition.disposition != Disposition.CUSTOMER_CONCESSION:
            raise InvalidDisposition("Only a customer special concession needs a customer approval.")
        if disposition.is_executed:
            raise InvalidDisposition("This disposition has already been executed.")
        if disposition.customer_approval_file:
            raise InvalidDisposition("A customer approval was already uploaded and cannot be replaced.")
        disposition.customer_approval_file.save(f"{uuid.uuid4().hex}.{kind}", ContentFile(data), save=False)
        disposition.save()
        _audit(user, disposition, "customer_approval_file", "", disposition.customer_approval_file.name,
               action=AuditLog.Action.CREATE, remarks="Customer written approval uploaded")
        executed = _try_execute(disposition, user, now)
    return executed


def execution_blockers(disposition):
    """What the Board still needs before the disposition is executed."""
    missing = [Role(r).label for r in MRB_ROLES if not disposition.signoffs.filter(role=r).exists()]
    out = [f"Waiting for sign-off: {', '.join(missing)}."] if missing else []
    if disposition.disposition == Disposition.CUSTOMER_CONCESSION and not disposition.customer_approval_file:
        out.append("Waiting for the customer's written approval to be uploaded.")
    return out


def _try_execute(disposition, user, now):
    if disposition.is_executed or execution_blockers(disposition):
        return False
    quarantine = disposition.quarantine
    traveler = quarantine.traveler
    kind = disposition.disposition
    if kind == Disposition.REWORK:
        disposition.rework_traveler = _create_rework_traveler(traveler, disposition.rework_from_op, user)
        _set_traveler_status(traveler, JobTraveler.Status.CLOSED, user, "Rework traveler issued")
    elif kind in (Disposition.SCRAP, Disposition.RTV):
        _set_traveler_status(traveler, JobTraveler.Status.CLOSED, user, f"{disposition.get_disposition_display()} executed")
        if kind == Disposition.RTV:
            notify_role(
                Role.DOCK_CLERK,
                subject=f"Return to vendor: {traveler.work_order.number} (debit note {disposition.debit_note_ref})",
                body=f"The MRB approved returning the lot to the vendor. Debit note request {disposition.debit_note_ref}. "
                     f"{disposition.notes}",
                related=disposition,
            )
    else:  # customer special concession: the lot is released
        _set_traveler_status(traveler, JobTraveler.Status.ACTIVE, user, "Customer special concession approved")
        if quarantine.inspection.checklist_type == ChecklistType.PDI:
            production.complete_final_pdi(
                _final_pdi_operation(traveler), user, quarantine.inspection, now
            )
    disposition.decided_at = now
    disposition.save()
    quarantine.status = QuarantineRecord.Status.CLOSED
    quarantine.closed_at = now
    quarantine.save()
    _audit(user, disposition, "decided_at", "", now,
           remarks=f"MRB disposition executed: {disposition.get_disposition_display()}")
    return True


def _set_traveler_status(traveler, status, user, remarks):
    previous = traveler.status
    traveler.status = status
    traveler.save(update_fields=["status"])
    _audit(user, traveler, "status", previous, status, remarks=remarks)


def _create_rework_traveler(original, from_op, user):
    """PRD 6.3: a rework traveler that repeats the routing from the chosen operation onward."""
    rework = JobTraveler.objects.create(
        work_order=original.work_order, quantity=original.quantity, is_rework=True, parent_traveler=original,
    )
    steps = [r for r in original.work_order.part.routing.all() if r.op_number >= from_op]
    TravelerOperation.objects.bulk_create(TravelerOperation(traveler=rework, routing_operation=s) for s in steps)
    log_event(
        action=AuditLog.Action.CREATE, user=user, instance=rework,
        remarks=f"Rework traveler from op {from_op} for {original}",
    )
    return rework
