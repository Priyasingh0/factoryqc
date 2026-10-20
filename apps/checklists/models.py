from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from apps.plant.models import GaugeType, Part

from .exceptions import InvalidTransition, TemplateLockedError
from .validators import validate_pdf


class ChecklistType(models.TextChoices):
    PDI = "PDI", "Pre-Dispatch Inspection"
    IN_PROCESS = "IN_PROCESS", "In-Process"
    FIRST_PIECE = "FIRST_PIECE", "First-Piece"


class TemplateStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    RELEASED = "RELEASED", "Released"
    SUPERSEDED = "SUPERSEDED", "Superseded"


# Allowed workflow moves (PRD 4.1). Anything else is rejected by save().
ALLOWED_TRANSITIONS = {
    TemplateStatus.DRAFT: {TemplateStatus.PENDING_APPROVAL},
    TemplateStatus.PENDING_APPROVAL: {TemplateStatus.DRAFT, TemplateStatus.RELEASED},
    TemplateStatus.RELEASED: {TemplateStatus.SUPERSEDED},
    TemplateStatus.SUPERSEDED: set(),
}

# The only columns that may change once a template has left Draft.
WORKFLOW_FIELDS = {"status", "approved_by", "approved_at", "review_notes"}


class DraftOnlyQuerySet(models.QuerySet):
    """Blocks bulk update/delete of rows that belong to a non-Draft template."""

    status_lookup = "status"

    def _assert_all_draft(self):
        if self.exclude(**{self.status_lookup: TemplateStatus.DRAFT}).exists():
            raise TemplateLockedError(
                "Released or pending templates cannot be changed. Create a new revision."
            )

    def update(self, **kwargs):
        self._assert_all_draft()
        return super().update(**kwargs)

    def delete(self):
        self._assert_all_draft()
        return super().delete()

    def bulk_update(self, objs, fields, batch_size=None):
        pks = [o.pk for o in objs if o.pk is not None]
        self.model._default_manager.filter(pk__in=pks)._assert_all_draft()
        return super().bulk_update(objs, fields, batch_size)


class TemplateQuerySet(DraftOnlyQuerySet):
    status_lookup = "status"


class ChildQuerySet(DraftOnlyQuerySet):
    status_lookup = "template__status"


revision_validator = RegexValidator(r"^[A-Z]{1,3}$", "Use capital letters, e.g. A, B, AA.")


def next_revision(revision):
    """A -> B, Z -> AA, AZ -> BA."""
    chars = list(revision.upper())
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "Z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "A"
        i -= 1
    return "A" + "".join(chars)


class ChecklistTemplate(models.Model):
    """Version-controlled PDI / in-process / first-piece template (PRD 4.1).

    Only a Draft may be edited. Releasing needs a Quality Manager (not the
    author). A drawing change means a new revision, never an edit.
    """

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="checklist_templates")
    checklist_type = models.CharField(max_length=15, choices=ChecklistType.choices)
    operation_number = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Routing operation (required for in-process and first-piece checklists).",
    )
    customer_agreement = models.CharField(max_length=200, blank=True)
    revision = models.CharField(max_length=3, default="A", validators=[revision_validator])
    status = models.CharField(
        max_length=20, choices=TemplateStatus.choices, default=TemplateStatus.DRAFT
    )
    drawing_pdf = models.FileField(
        upload_to="drawings/%Y/%m/", validators=[validate_pdf], blank=True
    )
    review_notes = models.TextField(blank=True, help_text="Reason given when a template is sent back.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="templates_created"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="templates_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TemplateQuerySet.as_manager()

    class Meta:
        ordering = ["part__part_number", "checklist_type", "operation_number", "revision"]
        permissions = [
            ("approve_checklisttemplate", "Can approve or reject checklist templates"),
        ]
        constraints = [
            # Two constraints because NULL operation numbers never compare equal in SQL.
            models.UniqueConstraint(
                fields=["part", "checklist_type", "revision"],
                condition=models.Q(operation_number__isnull=True),
                name="unique_template_revision_no_op",
            ),
            models.UniqueConstraint(
                fields=["part", "checklist_type", "operation_number", "revision"],
                condition=models.Q(operation_number__isnull=False),
                name="unique_template_revision_with_op",
            ),
        ]

    def __str__(self):
        op = f" Op{self.operation_number}" if self.operation_number else ""
        return f"{self.part.part_number} {self.get_checklist_type_display()}{op} Rev {self.revision}"

    @property
    def is_editable(self):
        return self.status == TemplateStatus.DRAFT

    def clean(self):
        super().clean()
        needs_op = self.checklist_type in (ChecklistType.IN_PROCESS, ChecklistType.FIRST_PIECE)
        if needs_op and not self.operation_number:
            raise ValidationError(
                {"operation_number": "Required for in-process and first-piece checklists."}
            )

    def _stored_status(self):
        return (
            type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = self._stored_status()
            if stored != TemplateStatus.DRAFT:
                update_fields = kwargs.get("update_fields")
                if update_fields is None or not set(update_fields) <= WORKFLOW_FIELDS:
                    raise TemplateLockedError(
                        "Released or pending templates cannot be edited. Create a new revision."
                    )
            if stored is not None and stored != self.status:
                if self.status not in ALLOWED_TRANSITIONS.get(stored, set()):
                    raise InvalidTransition(f"Cannot move a template from {stored} to {self.status}.")
        if self.status == TemplateStatus.RELEASED and not (self.approved_by_id and self.approved_at):
            raise InvalidTransition("A released template needs an approver and approval time.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != TemplateStatus.DRAFT:
            raise TemplateLockedError("Only a Draft template can be deleted.")
        return super().delete(*args, **kwargs)


class _DraftChildMixin(models.Model):
    """Rows owned by a template: frozen as soon as the template leaves Draft."""

    class Meta:
        abstract = True

    def _assert_template_is_draft(self):
        if self.template.status != TemplateStatus.DRAFT:
            raise TemplateLockedError(
                "This template is not a Draft, so its pins and fields cannot change. "
                "Create a new revision instead."
            )

    def save(self, *args, **kwargs):
        self._assert_template_is_draft()
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values_list("template_id", flat=True).first()
            if original is not None and original != self.template_id:
                raise TemplateLockedError("A pin or field cannot be moved to another template.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._assert_template_is_draft()
        return super().delete(*args, **kwargs)


class BlueprintPin(_DraftChildMixin):
    """Numbered, colour-coded marker on the drawing (PRD 4.2 Interactive Blueprint Pinning)."""

    template = models.ForeignKey(ChecklistTemplate, on_delete=models.CASCADE, related_name="pins")
    number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    page = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    x_pct = models.DecimalField(
        "X position (%)", max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    y_pct = models.DecimalField(
        "Y position (%)", max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    color = models.CharField(
        max_length=7, default="#2563eb",
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a hex colour like #2563eb.")],
    )

    objects = ChildQuerySet.as_manager()

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["template", "number"], name="unique_pin_number")
        ]

    def __str__(self):
        return f"Pin {self.number}"


class FieldType(models.TextChoices):
    DIMENSION = "DIMENSION", "Dimension with +/- tolerance"
    MAX = "MAX", "Maximum limit"
    MIN = "MIN", "Minimum limit"
    VISUAL = "VISUAL", "Visual pass/fail"


_REQUIRED_NUMBERS = {
    FieldType.DIMENSION: {"nominal", "upper_tol", "lower_tol"},
    FieldType.MAX: {"max_limit"},
    FieldType.MIN: {"min_limit"},
    FieldType.VISUAL: set(),
}
_NUMBER_FIELDS = ("nominal", "upper_tol", "lower_tol", "max_limit", "min_limit")


def _decimal_field(label):
    return models.DecimalField(label, max_digits=12, decimal_places=4, null=True, blank=True)


class TemplateField(_DraftChildMixin):
    """One inspection point on a template (PRD 4.2, 4.3)."""

    template = models.ForeignKey(ChecklistTemplate, on_delete=models.CASCADE, related_name="fields")
    sequence = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    label = models.CharField(max_length=150)
    pin = models.ForeignKey(
        BlueprintPin, on_delete=models.SET_NULL, null=True, blank=True, related_name="fields"
    )
    field_type = models.CharField(max_length=10, choices=FieldType.choices)
    nominal = _decimal_field("Nominal size")
    upper_tol = _decimal_field("Upper tolerance (+)")
    lower_tol = _decimal_field("Lower tolerance (-)")
    max_limit = _decimal_field("Maximum allowed")
    min_limit = _decimal_field("Minimum allowed")
    unit = models.CharField(max_length=10, blank=True)
    is_ctq = models.BooleanField("Critical-to-Quality", default=False)
    required_gauge_type = models.CharField(
        max_length=20, choices=GaugeType.choices, blank=True,
        help_text="Tool class the inspector must log for this reading (PRD 4.2).",
    )

    objects = ChildQuerySet.as_manager()

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["template", "sequence"], name="unique_field_sequence")
        ]

    def __str__(self):
        return f"{self.sequence}. {self.label}"

    def clean(self):
        super().clean()
        errors = {}
        required = _REQUIRED_NUMBERS.get(self.field_type)
        if required is None:  # field_type missing/invalid: the field-level error covers it
            return
        for name in _NUMBER_FIELDS:
            value = getattr(self, name)
            if name in required and value is None:
                errors[name] = "Required for this field type."
            elif name not in required and value is not None:
                errors[name] = "Not used for this field type; leave it empty."
        for name in ("upper_tol", "lower_tol"):
            value = getattr(self, name)
            if value is not None and value < 0:
                errors[name] = "Enter the size of the tolerance as a positive number."
        if self.field_type == FieldType.VISUAL:
            if self.required_gauge_type:
                errors["required_gauge_type"] = "Visual checks do not use a measuring tool."
            if self.unit:
                errors["unit"] = "Visual checks have no unit."
        else:
            if not self.required_gauge_type:
                errors["required_gauge_type"] = "Required: the inspector must log the tool used."
            if not self.unit:
                errors["unit"] = "Required for measured values."
        if self.pin_id and self.template_id and self.pin.template_id != self.template_id:
            errors["pin"] = "This pin belongs to a different template."
        if errors:
            raise ValidationError(errors)

    # -- limits and pass/fail (PRD 4.2, 5.3) ---------------------------------

    @property
    def lower_limit(self):
        if self.field_type == FieldType.DIMENSION:
            return self.nominal - self.lower_tol
        if self.field_type == FieldType.MIN:
            return self.min_limit
        return None

    @property
    def upper_limit(self):
        if self.field_type == FieldType.DIMENSION:
            return self.nominal + self.upper_tol
        if self.field_type == FieldType.MAX:
            return self.max_limit
        return None

    def evaluate(self, value):
        """True if `value` is within specification. Limits are inclusive.

        Visual fields take a bool (True = pass); all others take a number.
        """
        if self.field_type == FieldType.VISUAL:
            if not isinstance(value, bool):
                raise TypeError("A visual check needs a yes/no (bool) value.")
            return value
        if isinstance(value, bool):
            raise TypeError("A measured value must be a number.")
        value = value if isinstance(value, Decimal) else Decimal(str(value))
        low, high = self.lower_limit, self.upper_limit
        return (low is None or value >= low) and (high is None or value <= high)

    @property
    def spec_display(self):
        unit = f" {self.unit}" if self.unit else ""
        if self.field_type == FieldType.DIMENSION:
            return f"{self.nominal:.4f} (+{self.upper_tol:.4f} / -{self.lower_tol:.4f}){unit}"
        if self.field_type == FieldType.MAX:
            return f"{self.max_limit:.4f}{unit} max"
        if self.field_type == FieldType.MIN:
            return f"{self.min_limit:.4f}{unit} min"
        return "Pass / Fail"
