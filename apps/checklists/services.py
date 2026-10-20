"""Checklist template workflow (PRD 4.1): Draft -> Pending approval -> Released.

Views call these functions; the rules live here and in the models.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.audit import log_event
from apps.core.models import AuditLog

from .exceptions import (
    InvalidTransition,
    NotAuthorized,
    TemplateNotAvailable,
    TemplateValidationError,
)
from .models import (
    BlueprintPin,
    ChecklistTemplate,
    TemplateField,
    TemplateStatus,
    next_revision,
)

PERM_CHANGE = "checklists.change_checklisttemplate"
PERM_ADD = "checklists.add_checklisttemplate"
PERM_APPROVE = "checklists.approve_checklisttemplate"


def _require(user, perm, action):
    if not user.has_perm(perm):
        raise NotAuthorized(f"Your role is not allowed to {action}.")


def _expect_status(template, expected, action):
    if template.status != expected:
        raise InvalidTransition(
            f"Cannot {action}: the template is {template.get_status_display()}, "
            f"not {TemplateStatus(expected).label}."
        )


def _audit_status(template, user, previous, action, remarks=""):
    log_event(
        action=action, user=user, instance=template, field_name="status",
        previous_value=previous, new_value=template.status, remarks=remarks,
    )


# ------------------------------------------------------------- submit

def validation_problems(template):
    """Everything that stops a Draft being sent for approval (empty list = OK)."""
    problems = []
    try:
        template.full_clean()
    except ValidationError as exc:
        for name, messages_ in exc.message_dict.items():
            problems.extend(f"{name}: {m}" for m in messages_)
    if not template.drawing_pdf:
        problems.append("A PDF drawing must be uploaded.")
    fields = list(template.fields.select_related("pin"))
    if not fields:
        problems.append("Add at least one inspection field.")
    for field in fields:
        try:
            field.full_clean()
        except ValidationError as exc:
            for name, messages_ in exc.message_dict.items():
                problems.append(f"Field {field.sequence} ({field.label}) {name}: {' '.join(messages_)}")
    return problems


@transaction.atomic
def submit_for_approval(template, user):
    """Quality Engineer sends a complete Draft to the Quality Manager."""
    _require(user, PERM_CHANGE, "submit templates")
    _expect_status(template, TemplateStatus.DRAFT, "submit")
    problems = validation_problems(template)
    if problems:
        raise TemplateValidationError(problems)
    template.status = TemplateStatus.PENDING_APPROVAL
    template.save(update_fields=["status"])
    _audit_status(template, user, TemplateStatus.DRAFT, AuditLog.Action.UPDATE)
    return template


# ------------------------------------------------- approve / reject

@transaction.atomic
def approve_template(template, user):
    """Quality Manager releases the template for the shopfloor (PRD 4.1, 12).

    The author cannot approve their own template. Releasing supersedes the
    previous Released revision of the same part / type / operation.
    """
    _require(user, PERM_APPROVE, "approve templates")
    _expect_status(template, TemplateStatus.PENDING_APPROVAL, "approve")
    if template.created_by_id == user.pk:
        raise NotAuthorized("You cannot approve a template you created.")

    older = ChecklistTemplate.objects.filter(
        part=template.part,
        checklist_type=template.checklist_type,
        operation_number=template.operation_number,
        status=TemplateStatus.RELEASED,
    ).exclude(pk=template.pk)
    for previous in older:
        previous.status = TemplateStatus.SUPERSEDED
        previous.save(update_fields=["status"])
        _audit_status(
            previous, user, TemplateStatus.RELEASED, AuditLog.Action.UPDATE,
            remarks=f"Superseded by Rev {template.revision}",
        )

    template.status = TemplateStatus.RELEASED
    template.approved_by = user
    template.approved_at = timezone.now()
    template.review_notes = ""
    template.save(update_fields=["status", "approved_by", "approved_at", "review_notes"])
    _audit_status(template, user, TemplateStatus.PENDING_APPROVAL, AuditLog.Action.DECISION,
                  remarks="Approved and released")
    return template


@transaction.atomic
def reject_template(template, user, reason):
    """Quality Manager sends a pending template back to Draft with a reason."""
    _require(user, PERM_APPROVE, "reject templates")
    _expect_status(template, TemplateStatus.PENDING_APPROVAL, "reject")
    reason = (reason or "").strip()
    if not reason:
        raise TemplateValidationError(["Give a reason so the engineer knows what to fix."])
    template.status = TemplateStatus.DRAFT
    template.review_notes = reason
    template.save(update_fields=["status", "review_notes"])
    _audit_status(template, user, TemplateStatus.PENDING_APPROVAL, AuditLog.Action.DECISION,
                  remarks=f"Rejected: {reason}")
    return template


# -------------------------------------------------------- new revision

@transaction.atomic
def create_revision(template, user):
    """Copy a Released template into a new Draft with the next revision letter.

    This is the only way to change a released template (PRD 4.1): the old
    revision stays in force until the new one is approved.
    """
    _require(user, PERM_ADD, "create template revisions")
    _expect_status(template, TemplateStatus.RELEASED, "revise")

    siblings = ChecklistTemplate.objects.filter(
        part=template.part,
        checklist_type=template.checklist_type,
        operation_number=template.operation_number,
    )
    if siblings.filter(status__in=[TemplateStatus.DRAFT, TemplateStatus.PENDING_APPROVAL]).exists():
        raise InvalidTransition("A newer revision is already being prepared for this checklist.")
    latest = max((s.revision for s in siblings), key=lambda r: (len(r), r))

    new = ChecklistTemplate.objects.create(
        part=template.part,
        checklist_type=template.checklist_type,
        operation_number=template.operation_number,
        customer_agreement=template.customer_agreement,
        revision=next_revision(latest),
        drawing_pdf=template.drawing_pdf.name,  # same stored file; upload a new one if the drawing changed
        created_by=user,
    )
    pin_map = {}
    for pin in template.pins.all():
        pin_map[pin.pk] = BlueprintPin.objects.create(
            template=new, number=pin.number, page=pin.page,
            x_pct=pin.x_pct, y_pct=pin.y_pct, color=pin.color,
        )
    for field in template.fields.all():
        TemplateField.objects.create(
            template=new, sequence=field.sequence, label=field.label,
            pin=pin_map.get(field.pin_id), field_type=field.field_type,
            nominal=field.nominal, upper_tol=field.upper_tol, lower_tol=field.lower_tol,
            max_limit=field.max_limit, min_limit=field.min_limit, unit=field.unit,
            is_ctq=field.is_ctq, required_gauge_type=field.required_gauge_type,
        )
    log_event(
        action=AuditLog.Action.CREATE, user=user, instance=new,
        new_value=str(new), remarks=f"New revision copied from Rev {template.revision}",
    )
    return new


# ---------------------------------------------- use on the shopfloor

def assert_template_usable(template):
    """Only a Released template may be used for inspections (PRD 4.1)."""
    if template.status != TemplateStatus.RELEASED:
        raise TemplateNotAvailable(
            f"{template} is {template.get_status_display()}; only Released templates can be used."
        )


def get_active_template(part, checklist_type, operation_number=None):
    """The Released template for this part / type / operation, or TemplateNotAvailable."""
    template = ChecklistTemplate.objects.filter(
        part=part, checklist_type=checklist_type, operation_number=operation_number,
        status=TemplateStatus.RELEASED,
    ).first()
    if template is None:
        raise TemplateNotAvailable(
            f"No released {checklist_type} template exists for {part.part_number}."
        )
    return template
