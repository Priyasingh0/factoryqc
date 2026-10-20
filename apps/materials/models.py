from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.signing import QRKind, make_token
from apps.production.models import WorkOrder

from .exceptions import RecordLocked


class InsertOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise RecordLocked("Material records cannot be updated in bulk.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise RecordLocked("Material records cannot be updated in bulk.")

    def delete(self):
        raise RecordLocked("Material records cannot be deleted.")


class InsertOnlyModel(models.Model):
    """Rows are written once by a service and never changed or removed (PRD 13.3)."""

    objects = InsertOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RecordLocked(f"A {self._meta.verbose_name} cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked(f"A {self._meta.verbose_name} cannot be deleted.")


# --------------------------------------------------------------- master data

class Supplier(models.Model):
    """Approved raw-material supplier (PRD 3.1)."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120, unique=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True, help_text="Untick to stop new intakes from this supplier.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class MaterialGrade(models.Model):
    """Raw-material grade (e.g. EN8 bar, SS304 billet) with its engineering specification (PRD 3.2)."""

    code = models.CharField(max_length=30, unique=True)
    description = models.CharField(max_length=150, blank=True)
    requires_lab_testing = models.BooleanField(
        default=False, help_text="Lots of this grade go to internal spectro / physical testing before release (PRD 3.2)."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class GradeProperty(models.Model):
    """One chemical or mechanical limit the MTC must meet (PRD 3.2): e.g. Carbon 0.36-0.44 %."""

    class Kind(models.TextChoices):
        CHEMICAL = "CHEMICAL", "Chemical composition"
        MECHANICAL = "MECHANICAL", "Mechanical property"

    grade = models.ForeignKey(MaterialGrade, on_delete=models.CASCADE, related_name="properties")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    unit = models.CharField(max_length=15, blank=True)
    min_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    max_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        ordering = ["kind", "name"]
        verbose_name_plural = "grade properties"
        constraints = [models.UniqueConstraint(fields=["grade", "name"], name="unique_property_per_grade")]

    def __str__(self):
        return f"{self.grade.code} {self.name}"

    def clean(self):
        if self.min_value is None and self.max_value is None:
            raise ValidationError("Enter a minimum, a maximum, or both.")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValidationError("The minimum cannot be above the maximum.")

    def evaluate(self, value):
        """True if `value` meets the specification. Limits are inclusive."""
        value = value if isinstance(value, Decimal) else Decimal(str(value))
        return (self.min_value is None or value >= self.min_value) and (
            self.max_value is None or value <= self.max_value
        )

    @property
    def spec_display(self):
        unit = f" {self.unit}" if self.unit else ""
        if self.min_value is not None and self.max_value is not None:
            return f"{self.min_value:.4f} - {self.max_value:.4f}{unit}"
        if self.min_value is not None:
            return f"{self.min_value:.4f}{unit} min"
        return f"{self.max_value:.4f}{unit} max"


# ------------------------------------------------------------------- intake

class IntakeStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received (awaiting MTC review)"
    IN_LAB_TESTING = "IN_LAB_TESTING", "In lab testing"
    CLEARED = "CLEARED", "Cleared for production"
    SUPPLIER_QUARANTINE = "SUPPLIER_QUARANTINE", "Supplier quarantine"
    RETURNED = "RETURNED", "Returned to vendor"
    SCRAPPED = "SCRAPPED", "Scrapped"


# Allowed moves (PRD 3.2, 3.3). Done only by services; quarantined stock can leave
# quarantine only by being returned or scrapped, never back into production.
ALLOWED_TRANSITIONS = {
    IntakeStatus.RECEIVED: {IntakeStatus.IN_LAB_TESTING, IntakeStatus.CLEARED, IntakeStatus.SUPPLIER_QUARANTINE},
    IntakeStatus.IN_LAB_TESTING: {IntakeStatus.CLEARED, IntakeStatus.SUPPLIER_QUARANTINE},
    IntakeStatus.CLEARED: {IntakeStatus.SUPPLIER_QUARANTINE},
    IntakeStatus.SUPPLIER_QUARANTINE: {IntakeStatus.RETURNED, IntakeStatus.SCRAPPED},
    IntakeStatus.RETURNED: set(),
    IntakeStatus.SCRAPPED: set(),
}


class IntakeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise RecordLocked("Intake records cannot be updated in bulk.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise RecordLocked("Intake records cannot be updated in bulk.")

    def delete(self):
        raise RecordLocked("Intake records cannot be deleted.")


class RawMaterialIntake(models.Model):
    """One delivery of raw material, from dock receipt to clearance or quarantine (PRD 3)."""

    class Unit(models.TextChoices):
        KG = "KG", "kg"
        PCS = "PCS", "pieces"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="intakes")
    po_number = models.CharField("Purchase order", max_length=50)
    grade = models.ForeignKey(MaterialGrade, on_delete=models.PROTECT, related_name="intakes")
    heat_number = models.CharField(max_length=50)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    unit = models.CharField(max_length=3, choices=Unit.choices, default=Unit.KG)
    lab_required = models.BooleanField(editable=False, help_text="Copied from the grade when received.")
    mtc_file = models.FileField(upload_to="mtc/%Y/%m/")
    mtc_sha256 = models.CharField(max_length=64, editable=False)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="intakes_received")
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=IntakeStatus.choices, default=IntakeStatus.RECEIVED)
    mtc_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="mtcs_verified"
    )
    mtc_verified_at = models.DateTimeField(null=True, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)

    objects = IntakeQuerySet.as_manager()

    _CORE_FIELDS = (
        "supplier_id", "po_number", "grade_id", "heat_number", "quantity", "unit", "lab_required",
        "mtc_file", "mtc_sha256", "received_by_id", "received_at",
    )

    class Meta:
        ordering = ["-received_at", "-id"]
        default_permissions = ("view",)  # created and changed by services only
        permissions = [
            ("create_intake", "Can log raw material intake at the dock"),
            ("reject_intake", "Can reject an intake (supplier quarantine + SNCR)"),
            ("review_mtc", "Can review the MTC against the grade specification"),
            ("record_lab_result", "Can record internal lab test results"),
            ("quarantine_lot", "Can quarantine an already cleared lot (supplier recall)"),
            ("resolve_sncr", "Can close an SNCR by return-to-vendor or scrap"),
            ("issue_material", "Can issue cleared raw material to a work order"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["supplier", "heat_number", "po_number"], name="one_intake_per_heat_and_po"),
        ]

    def __str__(self):
        return f"{self.lot_number} (heat {self.heat_number})"

    # -- identifiers ---------------------------------------------------------

    @property
    def lot_number(self):
        return f"RM-{self.pk:06d}" if self.pk else "RM-new"

    @property
    def sample_tag(self):
        return f"{self.lot_number}-S" if self.pk else ""

    @property
    def barcode(self):
        """Signed barcode on the bundle label, printed only once the lot is cleared (PRD 3.2, 13.1)."""
        return make_token(QRKind.MATERIAL, self.pk) if self.pk else ""

    @property
    def is_usable(self):
        return self.status == IntakeStatus.CLEARED

    @property
    def issued_quantity(self):
        return self.issues.aggregate(total=models.Sum("quantity"))["total"] or Decimal("0")

    @property
    def remaining_quantity(self):
        return self.quantity - self.issued_quantity

    # -- protection ----------------------------------------------------------

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).first()
            if stored is None:
                raise RecordLocked("This intake no longer exists.")
            changed = [f for f in self._CORE_FIELDS if getattr(stored, f) != getattr(self, f)]
            if changed:
                raise RecordLocked("The received data of an intake cannot be edited.")
            if self.status != stored.status and self.status not in ALLOWED_TRANSITIONS[stored.status]:
                raise RecordLocked(f"A lot cannot move from {stored.status} to {self.status}.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked("An intake record cannot be deleted.")


class MTCReading(InsertOnlyModel):
    """One certified value from the MTC, checked against the grade limit (PRD 3.2)."""

    intake = models.ForeignKey(RawMaterialIntake, on_delete=models.PROTECT, related_name="readings")
    grade_property = models.ForeignKey(GradeProperty, on_delete=models.PROTECT, related_name="readings")
    value = models.DecimalField(max_digits=12, decimal_places=4)
    spec_min = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, editable=False)
    spec_max = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, editable=False)
    is_within_spec = models.BooleanField(editable=False)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="mtc_readings")
    entered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)
        ordering = ["grade_property__kind", "grade_property__name"]
        constraints = [models.UniqueConstraint(fields=["intake", "grade_property"], name="one_reading_per_property")]

    def __str__(self):
        return f"{self.grade_property.name} = {self.value}"

    @property
    def spec_display(self):
        """The limits that applied when the value was checked."""
        unit = f" {self.grade_property.unit}" if self.grade_property.unit else ""
        if self.spec_min is not None and self.spec_max is not None:
            return f"{self.spec_min:.4f} - {self.spec_max:.4f}{unit}"
        if self.spec_min is not None:
            return f"{self.spec_min:.4f}{unit} min"
        return f"{self.spec_max:.4f}{unit} max"

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.grade_property.grade_id != self.intake.grade_id:
                raise RecordLocked("That property belongs to a different material grade.")
            # Always computed here against the limits in force today, never trusted from the caller.
            self.spec_min, self.spec_max = self.grade_property.min_value, self.grade_property.max_value
            self.is_within_spec = self.grade_property.evaluate(self.value)
        super().save(*args, **kwargs)


class LabResult(InsertOnlyModel):
    """Internal spectro / physical test outcome for a lab-tested lot (PRD 3.2)."""

    intake = models.OneToOneField(RawMaterialIntake, on_delete=models.PROTECT, related_name="lab_result")
    passed = models.BooleanField()
    report_reference = models.CharField(max_length=80)
    remarks = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="lab_results")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)

    def __str__(self):
        return f"Lab {'pass' if self.passed else 'fail'} for {self.intake}"


# ------------------------------------------------------------------- SNCR

class SNCR(models.Model):
    """Supplier Non-Conformance Report, raised automatically when stock is quarantined (PRD 3.3)."""

    class Reason(models.TextChoices):
        DIMENSION = "DIMENSION", "Failed physical dimension check"
        SPECTRO = "SPECTRO", "Failed spectro chemical test"
        NO_VALID_MTC = "NO_VALID_MTC", "Missing or invalid MTC"
        MTC_OUT_OF_SPEC = "MTC_OUT_OF_SPEC", "MTC values outside the grade specification"
        LAB_FAIL = "LAB_FAIL", "Failed internal lab test"
        DAMAGED = "DAMAGED", "Damaged or wrong material"
        RECALL = "RECALL", "Supplier recall / defect found after clearance"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    class Resolution(models.TextChoices):
        RTV = "RTV", "Return to vendor (debit note requested)"
        SCRAP = "SCRAP", "Scrap"

    intake = models.OneToOneField(RawMaterialIntake, on_delete=models.PROTECT, related_name="sncr")
    reason_code = models.CharField(max_length=20, choices=Reason.choices)
    details = models.TextField()
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sncrs_raised")
    raised_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    resolution = models.CharField(max_length=5, choices=Resolution.choices, blank=True)
    resolution_reference = models.CharField(
        max_length=60, blank=True, help_text="Debit note reference (RTV) or scrap cost code (scrap)."
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="sncrs_closed"
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = InsertOnlyQuerySet.as_manager()

    _CORE_FIELDS = ("intake_id", "reason_code", "details", "raised_by_id", "raised_at")

    class Meta:
        ordering = ["-raised_at", "-id"]
        default_permissions = ("view",)
        verbose_name = "SNCR"

    def __str__(self):
        return f"{self.number} ({self.get_status_display()})"

    @property
    def number(self):
        return f"SNCR-{self.pk:05d}" if self.pk else "SNCR-new"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).first()
            if stored is None or any(getattr(stored, f) != getattr(self, f) for f in self._CORE_FIELDS):
                raise RecordLocked("An SNCR cannot be edited.")
            if stored.status != self.Status.OPEN or self.status != self.Status.CLOSED:
                raise RecordLocked("An SNCR can only be closed, once.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked("An SNCR cannot be deleted.")


# ---------------------------------------------------------- work-order link

class MaterialIssue(InsertOnlyModel):
    """Cleared stock issued to a work order: the heat-lot to work-order link (PRD 7.2)."""

    intake = models.ForeignKey(RawMaterialIntake, on_delete=models.PROTECT, related_name="issues")
    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="material_issues")
    quantity = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="material_issues")
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)
        ordering = ["-issued_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["intake", "work_order"], name="one_issue_per_lot_and_order")]

    def __str__(self):
        return f"{self.quantity} from {self.intake.lot_number} to {self.work_order.number}"
