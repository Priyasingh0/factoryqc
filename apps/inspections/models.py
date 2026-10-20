from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.checklists.models import ChecklistTemplate, ChecklistType, FieldType, TemplateField
from apps.plant.models import Gauge, Machine
from apps.production.models import JobTraveler, TravelerOperation

from .exceptions import InspectionLocked


class InspectionStatus(models.TextChoices):
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    SUBMITTED = "SUBMITTED", "Awaiting approval"
    FROZEN = "FROZEN", "Frozen (CTQ failure)"
    APPROVED = "APPROVED", "Approved"
    CONCESSION = "CONCESSION", "Released on concession"
    QUARANTINED = "QUARANTINED", "Quarantined"


# Allowed status moves (PRD 4.3, 6.1). The decisions after SUBMITTED / FROZEN are
# made only by apps/review/services.py; every final state is locked for good.
ALLOWED_TRANSITIONS = {
    InspectionStatus.IN_PROGRESS: {InspectionStatus.SUBMITTED, InspectionStatus.FROZEN},
    InspectionStatus.SUBMITTED: {
        InspectionStatus.APPROVED, InspectionStatus.CONCESSION, InspectionStatus.QUARANTINED,
    },
    InspectionStatus.FROZEN: {InspectionStatus.QUARANTINED},
    InspectionStatus.APPROVED: set(),
    InspectionStatus.CONCESSION: set(),
    InspectionStatus.QUARANTINED: set(),
}


class ImmutableQuerySet(models.QuerySet):
    """Blocks every bulk way of changing or removing inspection records (PRD 13.3)."""

    def update(self, **kwargs):
        raise InspectionLocked("Inspection records cannot be updated in bulk.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise InspectionLocked("Inspection records cannot be updated in bulk.")

    def delete(self):
        raise InspectionLocked("Inspection records cannot be deleted.")


def _stored_status(inspection_id):
    return Inspection.objects.filter(pk=inspection_id).values_list("status", flat=True).first()


class Inspection(models.Model):
    """One inspector's run through a Released checklist for one lot (PRD 5)."""

    template = models.ForeignKey(ChecklistTemplate, on_delete=models.PROTECT, related_name="inspections")
    checklist_type = models.CharField(max_length=12, choices=ChecklistType.choices)
    traveler = models.ForeignKey(JobTraveler, on_delete=models.PROTECT, related_name="inspections")
    traveler_operation = models.ForeignKey(
        TravelerOperation, on_delete=models.PROTECT, related_name="inspections",
        help_text="The routing step being inspected (the run for a first piece, the final PDI step for a PDI).",
    )
    machine = models.ForeignKey(
        Machine, on_delete=models.PROTECT, related_name="inspections",
        help_text="Station whose QR plate the inspector scanned (PRD 5.1).",
    )
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inspections"
    )
    status = models.CharField(
        max_length=12, choices=InspectionStatus.choices, default=InspectionStatus.IN_PROGRESS
    )
    started_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_reason = models.TextField(blank=True)
    remarks = models.TextField("Inspector remarks", blank=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["-started_at", "-id"]
        default_permissions = ("view",)  # no add/change/delete: services only
        permissions = [("conduct_inspection", "Can run inspections, enter readings and upload defect photos")]
        constraints = [
            # PRD 5.1: the tablet locks on one lot until the checklist is finished.
            models.UniqueConstraint(
                fields=["inspector"], condition=Q(status="IN_PROGRESS"),
                name="one_open_inspection_per_inspector",
            ),
            models.UniqueConstraint(
                fields=["traveler_operation", "checklist_type"], condition=Q(status="IN_PROGRESS"),
                name="one_open_inspection_per_step_and_type",
            ),
        ]

    def __str__(self):
        return f"Inspection #{self.pk} ({self.get_checklist_type_display()}, {self.traveler.work_order.number})"

    @property
    def is_open(self):
        return self.status == InspectionStatus.IN_PROGRESS

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = _stored_status(self.pk)
            if stored is None:
                raise InspectionLocked("This inspection no longer exists.")
            if stored != InspectionStatus.IN_PROGRESS:
                # Locked for good; only a defined workflow move may touch it.
                if self.status == stored or self.status not in ALLOWED_TRANSITIONS.get(stored, set()):
                    raise InspectionLocked("A submitted or frozen inspection cannot be changed.")
            elif self.status != stored and self.status not in ALLOWED_TRANSITIONS[stored]:
                raise InspectionLocked(f"Cannot move an inspection from {stored} to {self.status}.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise InspectionLocked("Inspection records cannot be deleted.")

    # -- results ---------------------------------------------------------------

    @property
    def failed_measurements(self):
        return self.measurements.filter(is_within_tolerance=False)

    @property
    def all_within_tolerance(self):
        return not self.failed_measurements.exists()


def _ensure_open_or_frozen_insert(inspection_id, allow_frozen):
    stored = _stored_status(inspection_id)
    allowed = {InspectionStatus.IN_PROGRESS}
    if allow_frozen:
        allowed.add(InspectionStatus.FROZEN)
    if stored not in allowed:
        raise InspectionLocked("This inspection is closed; nothing more can be added to it.")


class Measurement(models.Model):
    """One reading against one template field (PRD 5.2, 5.3). Insert-only."""

    inspection = models.ForeignKey(Inspection, on_delete=models.PROTECT, related_name="measurements")
    field = models.ForeignKey(TemplateField, on_delete=models.PROTECT, related_name="measurements")
    value_numeric = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    value_bool = models.BooleanField(null=True, blank=True, help_text="Visual checks: True = pass.")
    gauge = models.ForeignKey(
        Gauge, on_delete=models.PROTECT, null=True, blank=True, related_name="measurements",
        help_text="The exact tool used (PRD 2.3, 4.2). Empty for visual checks.",
    )
    is_within_tolerance = models.BooleanField(editable=False)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="measurements_entered"
    )
    entered_at = models.DateTimeField(default=timezone.now)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["field__sequence"]
        default_permissions = ("view",)
        constraints = [
            models.UniqueConstraint(fields=["inspection", "field"], name="one_measurement_per_field"),
            models.CheckConstraint(
                name="measurement_value_matches_kind",
                condition=(
                    Q(value_bool__isnull=False, value_numeric__isnull=True, gauge__isnull=True)
                    | Q(value_bool__isnull=True, value_numeric__isnull=False, gauge__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return f"{self.field.label}: {self.display_value}"

    @property
    def display_value(self):
        if self.value_bool is not None:
            return "Pass" if self.value_bool else "Fail"
        return f"{self.value_numeric:.4f} {self.field.unit}".strip()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise InspectionLocked("A recorded measurement cannot be changed.")
        _ensure_open_or_frozen_insert(self.inspection_id, allow_frozen=False)
        if self.field.template_id != self.inspection.template_id:
            raise ValidationError("This field does not belong to the inspection's checklist.")
        if self.field.field_type == FieldType.VISUAL:
            if self.value_bool is None or self.value_numeric is not None:
                raise ValidationError("A visual check needs a pass/fail answer only.")
            value = self.value_bool
        else:
            if self.value_numeric is None or self.value_bool is not None:
                raise ValidationError("A measured field needs a number only.")
            value = Decimal(self.value_numeric)
        # Always computed here, never trusted from the caller (PRD 5.3).
        self.is_within_tolerance = self.field.evaluate(value)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise InspectionLocked("A recorded measurement cannot be deleted.")


class DefectPhoto(models.Model):
    """Photo proof of an out-of-tolerance reading (PRD 5.4). Insert-only."""

    measurement = models.ForeignKey(Measurement, on_delete=models.PROTECT, related_name="photos")
    image = models.FileField(upload_to="defect_photos/%Y/%m/")
    sha256 = models.CharField(max_length=64, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="defect_photos"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["uploaded_at", "id"]
        default_permissions = ("view",)
        constraints = [
            # The same picture twice must not count as two photos.
            models.UniqueConstraint(fields=["measurement", "sha256"], name="unique_photo_per_measurement"),
        ]

    def __str__(self):
        return f"Photo of {self.measurement}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise InspectionLocked("A defect photo cannot be changed.")
        if self.measurement.is_within_tolerance:
            raise ValidationError("Defect photos belong to out-of-tolerance readings only.")
        _ensure_open_or_frozen_insert(self.measurement.inspection_id, allow_frozen=True)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise InspectionLocked("A defect photo cannot be deleted.")
