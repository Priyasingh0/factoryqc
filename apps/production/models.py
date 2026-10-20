from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.signing import QRKind, make_token
from apps.plant.models import Machine, Part, Shift

from .exceptions import RecordLockedError


# ------------------------------------------------------------ work orders

class WorkOrderQuerySet(models.QuerySet):
    """Blocks bulk update/delete of released work orders."""

    def _assert_none_released(self):
        if self.filter(released_at__isnull=False).exists():
            raise RecordLockedError("A released work order cannot be changed or deleted.")

    def update(self, **kwargs):
        self._assert_none_released()
        return super().update(**kwargs)

    def delete(self):
        self._assert_none_released()
        return super().delete()


class WorkOrder(models.Model):
    """Production work order for a lot of one part (PRD 7.1).

    Releasing it generates the Digital Job Traveler. After release nothing on
    it may change, so the traveler always matches what was ordered.
    """

    number = models.CharField(max_length=30, unique=True)
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="work_orders")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    customer_po = models.CharField("Customer PO", max_length=50, blank=True)
    heat_number = models.CharField(
        max_length=50, blank=True,
        help_text="Raw-material heat lot (PRD 7.2). Linked to material intake records in a later step.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="work_orders_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="work_orders_released",
    )
    released_at = models.DateTimeField(null=True, blank=True)

    objects = WorkOrderQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        permissions = [("release_workorder", "Can release work orders and generate travelers")]

    def __str__(self):
        return f"{self.number} ({self.part.part_number} x {self.quantity})"

    @property
    def is_released(self):
        return self.released_at is not None

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).values_list("released_at", flat=True).first()
            if stored is not None:
                raise RecordLockedError("A released work order cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_released:
            raise RecordLockedError("A released work order cannot be deleted.")
        return super().delete(*args, **kwargs)


# ---------------------------------------------------------------- routing

class RoutingOperation(models.Model):
    """One step of a part's master process routing (PRD 7.1), e.g. Op 30 Heat Treatment."""

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="routing")
    op_number = models.PositiveIntegerField(
        validators=[MinValueValidator(1)], help_text="Steps run in ascending order: 10, 20, 30..."
    )
    name = models.CharField(max_length=100)
    hold_minutes = models.PositiveIntegerField(
        default=0, help_text="Mandatory cooling/curing hold after this operation (PRD 8.1). 0 = none."
    )
    requires_first_piece = models.BooleanField(
        default=False, help_text="QA must sign off the first piece before production (PRD 9.1)."
    )
    is_final_pdi = models.BooleanField(
        default=False, help_text="The final Pre-Dispatch Inspection step (completed via inspection approval)."
    )
    inspection_interval_minutes = models.PositiveIntegerField(
        null=True, blank=True, help_text="Periodic in-process check every N minutes (PRD 9.2)."
    )
    inspection_every_parts = models.PositiveIntegerField(
        null=True, blank=True, help_text="...and/or every N parts produced."
    )
    inspection_sample_size = models.PositiveIntegerField(
        default=3, validators=[MinValueValidator(1)], help_text="Parts to measure at each periodic check."
    )

    class Meta:
        ordering = ["part", "op_number"]
        constraints = [
            models.UniqueConstraint(fields=["part", "op_number"], name="unique_routing_op_number")
        ]

    def __str__(self):
        return f"Op {self.op_number}: {self.name}"

    @property
    def has_periodic_inspection(self):
        return bool(self.inspection_interval_minutes or self.inspection_every_parts)

    def clean(self):
        super().clean()
        if self.is_final_pdi and (
            self.requires_first_piece or self.has_periodic_inspection or self.hold_minutes
        ):
            raise ValidationError(
                "The final PDI step cannot have a hold, first-piece or periodic inspection."
            )

    def save(self, *args, **kwargs):
        if not self._state.adding and self.traveler_operations.exists():
            raise RecordLockedError(
                "This routing step is used by a released job traveler and cannot be changed."
            )
        super().save(*args, **kwargs)


# --------------------------------------------------------------- travelers

class JobTraveler(models.Model):
    """Digital Job Traveler for one lot (PRD 7.1). Created by releasing a work order."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        QUARANTINED = "QUARANTINED", "Quarantined"
        COMPLETED = "COMPLETED", "Completed"
        CLOSED = "CLOSED", "Closed (dispositioned by MRB)"

    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="travelers")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    is_rework = models.BooleanField(default=False)
    parent_traveler = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="rework_travelers"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        default_permissions = ("view",)  # created and changed by services only
        constraints = [
            models.UniqueConstraint(
                fields=["work_order"], condition=models.Q(is_rework=False),
                name="one_original_traveler_per_work_order",
            )
        ]

    def __str__(self):
        suffix = " (rework)" if self.is_rework else ""
        return f"Traveler {self.work_order.number}{suffix}"

    @property
    def barcode(self):
        """Signed barcode printed on the traveler sheet (PRD 5.1, 13.1)."""
        if self.pk is None:
            return ""
        return make_token(QRKind.TRAVELER, self.pk)


class TravelerOperation(models.Model):
    """One routing step on one traveler, with who did it, when, and where (PRD 7.1, 9.2)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"

    traveler = models.ForeignKey(JobTraveler, on_delete=models.PROTECT, related_name="operations")
    routing_operation = models.ForeignKey(
        RoutingOperation, on_delete=models.PROTECT, related_name="traveler_operations"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    machine = models.ForeignKey(
        Machine, on_delete=models.PROTECT, null=True, blank=True, related_name="traveler_operations"
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="operations_run",
    )
    shift = models.CharField(max_length=10, choices=Shift.choices, blank=True)
    clock_in = models.DateTimeField(null=True, blank=True)
    clock_out = models.DateTimeField(null=True, blank=True)
    cycle_seconds = models.PositiveIntegerField(
        null=True, blank=True, help_text="Active run time, excluding paused time (PRD 9.2)."
    )
    signed_off_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="operations_signed_off",
    )
    hold_until = models.DateTimeField(
        null=True, blank=True, help_text="Mandatory hold after this operation ends (PRD 8.2)."
    )

    class Meta:
        ordering = ["routing_operation__op_number"]
        default_permissions = ("view",)
        permissions = [("operate_traveleroperation", "Can start, count and finish operations")]
        constraints = [
            models.UniqueConstraint(
                fields=["traveler", "routing_operation"], name="unique_operation_per_traveler"
            )
        ]

    def __str__(self):
        return f"{self.traveler.work_order.number} {self.routing_operation}"

    def hold_remaining(self, now=None):
        """Time left on this operation's hold (zero if none or expired)."""
        now = now or timezone.now()
        if self.hold_until and self.hold_until > now:
            return self.hold_until - now
        return timedelta(0)


class ProductionRun(models.Model):
    """Live state of an operation: first-piece lock, part counter, periodic checks (PRD 9)."""

    class State(models.TextChoices):
        SETUP = "SETUP", "Setup mode (awaiting first-piece sign-off)"
        RUNNING = "RUNNING", "Running"

    traveler_operation = models.OneToOneField(
        TravelerOperation, on_delete=models.PROTECT, related_name="run"
    )
    state = models.CharField(max_length=10, choices=State.choices)
    first_piece_inspection = models.ForeignKey(
        "inspections.Inspection", on_delete=models.PROTECT, null=True, blank=True,
        related_name="first_piece_runs",
        help_text="Submitted first-piece inspection QA must have before signing off (PRD 9.1).",
    )
    first_piece_cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="first_pieces_cleared",
    )
    first_piece_cleared_at = models.DateTimeField(null=True, blank=True)
    parts_produced = models.PositiveIntegerField(default=0)
    parts_at_last_check = models.PositiveIntegerField(default=0)
    last_check_at = models.DateTimeField(null=True, blank=True)
    next_check_due_at = models.DateTimeField(null=True, blank=True)
    check_due_since = models.DateTimeField(
        null=True, blank=True, help_text="When the parts-based periodic check became due."
    )
    paused_seconds = models.PositiveIntegerField(
        default=0, help_text="Time the job timer was paused for an overdue periodic check."
    )

    class Meta:
        default_permissions = ("view",)
        permissions = [
            ("clear_first_piece", "Can sign off the first piece"),
            ("log_periodic_check", "Can log a periodic in-process inspection"),
        ]

    def __str__(self):
        return f"Run of {self.traveler_operation}"


class PartSerial(models.Model):
    """Serial number of one finished part, linking it to its traveler (PRD 7.2)."""

    serial_number = models.CharField(max_length=60, unique=True)
    traveler = models.ForeignKey(JobTraveler, on_delete=models.PROTECT, related_name="serials")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["serial_number"]
        default_permissions = ("view",)

    def __str__(self):
        return self.serial_number
