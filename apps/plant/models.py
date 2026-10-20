from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.signing import QRKind, make_token


class Shift(models.TextChoices):
    """Factory shifts. Hours are defined in services.shift_for()."""

    MORNING = "MORNING", "Morning"
    EVENING = "EVENING", "Evening"
    NIGHT = "NIGHT", "Night"


class GaugeType(models.TextChoices):
    """Measuring equipment types (PRD 2.1 item 4)."""

    CALIPER = "CALIPER", "Caliper"
    MICROMETER = "MICROMETER", "Micrometer"
    HEIGHT_GAUGE = "HEIGHT_GAUGE", "Height gauge"
    PLUG_GAUGE = "PLUG_GAUGE", "Plug gauge"
    CMM = "CMM", "CMM"
    OTHER = "OTHER", "Other"


class Department(models.Model):
    """Factory site / department, e.g. Main Machining Shop (PRD 2.1 item 1)."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Machine(models.Model):
    """Machine tool / workstation, e.g. CNC Lathe #04 (PRD 2.1 item 2)."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="machines"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    @property
    def qr_token(self):
        """Signed token printed on the station's QR plate (PRD 5.1, 13.1).

        Computed rather than stored, so it can never go stale or be edited.
        """
        if self.pk is None:
            return ""
        return make_token(QRKind.MACHINE, self.pk)


class MachinePMCheck(TimeStampedModel):
    """Daily / per-shift preventive-maintenance and safety zero-point check (PRD 2.2).

    One row per machine, date and shift. Created through
    services.record_pm_check() so every entry is validated and audited.
    """

    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name="pm_checks")
    date = models.DateField()
    shift = models.CharField(max_length=10, choices=Shift.choices)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pm_checks"
    )
    completed = models.BooleanField(
        default=False, help_text="Daily PM checklist completed."
    )
    safety_zero_point_ok = models.BooleanField(
        default=False, help_text="Safety zero-point check passed."
    )
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["machine", "date", "shift"], name="unique_pm_check_per_shift"
            )
        ]
        # No change/delete permission: records are written by the service only.
        default_permissions = ("add", "view")

    def __str__(self):
        return f"{self.machine.code} {self.date} {self.get_shift_display()}"

    @property
    def is_clear(self):
        return self.completed and self.safety_zero_point_ok


class Part(models.Model):
    """Manufactured component / part number (PRD 2.1 item 3)."""

    part_number = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)

    class Meta:
        ordering = ["part_number"]

    def __str__(self):
        return f"{self.part_number} - {self.name}"


class ToolingProgram(models.Model):
    """Approved CNC program, fixture setup and tool offsets for a part (PRD 2.1)."""

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="programs")
    program_name = models.CharField(max_length=100)
    version = models.CharField(max_length=20)
    fixture_setup = models.CharField(max_length=200, blank=True)
    tool_offsets = models.TextField(blank=True)
    is_approved = models.BooleanField(default=False)

    class Meta:
        ordering = ["part", "program_name", "version"]
        constraints = [
            models.UniqueConstraint(
                fields=["part", "program_name", "version"], name="unique_program_version"
            )
        ]

    def __str__(self):
        return f"{self.program_name} v{self.version}"


class Gauge(models.Model):
    """Measuring gauge in the equipment register (PRD 2.1 item 4, 2.3)."""

    class Status(models.TextChoices):
        VALID = "VALID", "Valid"
        EXPIRED = "EXPIRED", "Expired"
        INACTIVE = "INACTIVE", "Inactive"

    gauge_id = models.CharField(
        max_length=30, unique=True, help_text="Unique serial, e.g. VC-104."
    )
    gauge_type = models.CharField(max_length=20, choices=GaugeType.choices)
    description = models.CharField(max_length=150, blank=True)
    calibrated_on = models.DateField()
    calibration_expiry = models.DateField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["gauge_id"]

    def __str__(self):
        return f"{self.gauge_id} ({self.get_gauge_type_display()})"

    def clean(self):
        super().clean()
        if self.calibrated_on and self.calibration_expiry:
            if self.calibration_expiry < self.calibrated_on:
                raise ValidationError(
                    {"calibration_expiry": "Expiry cannot be before the calibration date."}
                )

    def is_calibrated_on(self, day):
        """Calibrated if the expiry date is on or after `day`."""
        return self.calibration_expiry >= day

    @property
    def is_calibrated(self):
        return self.is_calibrated_on(timezone.localdate())

    @property
    def status(self):
        if not self.is_active:
            return self.Status.INACTIVE
        if not self.is_calibrated:
            return self.Status.EXPIRED
        return self.Status.VALID
