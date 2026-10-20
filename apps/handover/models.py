from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.plant.models import Machine, Shift

from .exceptions import RecordLocked

SHIFT_ORDER = {Shift.MORNING: 0, Shift.EVENING: 1, Shift.NIGHT: 2}


class InsertOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise RecordLocked("Handover records cannot be updated in bulk.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise RecordLocked("Handover records cannot be updated in bulk.")

    def delete(self):
        raise RecordLocked("Handover records cannot be deleted.")


class InsertOnlyModel(models.Model):
    """Rows are written once by a service and never changed or removed (PRD 12.2, 13.3)."""

    objects = InsertOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RecordLocked(f"A {self._meta.verbose_name} cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked(f"A {self._meta.verbose_name} cannot be deleted.")


class ToolWear(models.TextChoices):
    OK = "OK", "Tools in good condition"
    WATCH = "WATCH", "Wear visible: watch closely"
    CHANGE_NOW = "CHANGE_NOW", "Change tooling before running"


class MachineHealth(models.TextChoices):
    GOOD = "GOOD", "Running normally"
    DEGRADED = "DEGRADED", "Running with a problem"
    DOWN = "DOWN", "Machine is down"


class HandoverStatus(models.TextChoices):
    AWAITING_SIGNOFF = "AWAITING_SIGNOFF", "Awaiting supervisor sign-off"
    AWAITING_ACK = "AWAITING_ACK", "Awaiting incoming acknowledgement"
    ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"


class ShiftHandover(InsertOnlyModel):
    """The Digital Shift Handover form for one machine at one shift change (PRD 12.2).

    The job status, parts, open quality alerts and pending reviews are read from the
    system when the form is saved, so the outgoing worker cannot misstate them.
    The worker enters scrap, tool wear and machine health. The supervisor
    countersign and the incoming acknowledgement are separate permanent rows.
    """

    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name="shift_handovers")
    shift_date = models.DateField()
    shift = models.CharField(max_length=10, choices=Shift.choices)
    outgoing_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="handovers_given"
    )
    created_at = models.DateTimeField(default=timezone.now)
    # Read from the system
    open_jobs = models.JSONField(default=list)
    parts_produced = models.PositiveIntegerField()
    open_quality_alerts = models.PositiveIntegerField()
    pending_pdi_reviews = models.PositiveIntegerField()
    # Entered by the outgoing worker
    scrap_count = models.PositiveIntegerField()
    job_status_notes = models.TextField(blank=True)
    tool_wear = models.CharField(max_length=12, choices=ToolWear.choices)
    tool_wear_notes = models.TextField(blank=True)
    machine_health = models.CharField(max_length=10, choices=MachineHealth.choices)
    machine_health_notes = models.TextField(blank=True)

    class Meta:
        default_permissions = ("view",)
        permissions = [
            ("create_shifthandover", "Can complete the outgoing shift handover form"),
            ("sign_shifthandover", "Can countersign a shift handover as supervisor"),
            ("acknowledge_shifthandover", "Can acknowledge a shift handover as the incoming worker"),
        ]
        ordering = ["-shift_date", "-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["machine", "shift_date", "shift"], name="one_handover_per_machine_shift"),
        ]

    def __str__(self):
        return f"Handover {self.machine.code} {self.shift_date} {self.get_shift_display()}"

    @property
    def shift_key(self):
        return (self.shift_date, SHIFT_ORDER[self.shift])

    @property
    def status(self):
        if not hasattr(self, "signoff"):
            return HandoverStatus.AWAITING_SIGNOFF
        if not hasattr(self, "acknowledgement"):
            return HandoverStatus.AWAITING_ACK
        return HandoverStatus.ACKNOWLEDGED

    @property
    def status_label(self):
        return HandoverStatus(self.status).label


class HandoverSignoff(InsertOnlyModel):
    """The outgoing supervisor countersigns the handover form (PRD 12.2)."""

    handover = models.OneToOneField(ShiftHandover, on_delete=models.PROTECT, related_name="signoff")
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="handovers_signed"
    )
    signed_at = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True)

    class Meta:
        default_permissions = ("view",)


class HandoverAcknowledgement(InsertOnlyModel):
    """The incoming worker confirms they reviewed the log before starting work (PRD 12.2)."""

    handover = models.OneToOneField(ShiftHandover, on_delete=models.PROTECT, related_name="acknowledgement")
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="handovers_acknowledged"
    )
    acknowledged_at = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True)

    class Meta:
        default_permissions = ("view",)
