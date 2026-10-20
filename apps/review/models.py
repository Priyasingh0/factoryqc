from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.accounts.models import Role
from apps.inspections.models import Inspection
from apps.production.models import JobTraveler

from .exceptions import RecordLocked

HOLDING_AREA = "Digital Rejection Holding Area"  # PRD 6.1

# PRD 6.3: the Material Review Board.
MRB_ROLES = (Role.PLANT_MANAGER, Role.QUALITY_HEAD, Role.PRODUCTION_LEAD)


class InsertOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise RecordLocked("Review records cannot be updated in bulk.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise RecordLocked("Review records cannot be updated in bulk.")

    def delete(self):
        raise RecordLocked("Review records cannot be deleted.")


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


class Decision(models.TextChoices):
    APPROVED = "APPROVED", "Approved"
    CONCESSION = "CONCESSION", "Conditional concession release"
    QUARANTINED = "QUARANTINED", "Quarantined"


class ReasonCode(models.TextChoices):
    """Concession reason codes (PRD 6.1)."""

    COSMETIC = "COSMETIC", "Cosmetic only, no effect on use"
    FIT_FORM_FUNCTION = "FIT_FORM_FUNCTION", "Fit, form and function not affected"
    CUSTOMER_DEVIATION = "CUSTOMER_DEVIATION", "Within customer-agreed deviation"
    REWORK_IMPRACTICAL = "REWORK_IMPRACTICAL", "Rework not practical, use as is"
    OTHER_ENGINEERING = "OTHER_ENGINEERING", "Other engineering justification"


class SupervisorDecision(InsertOnlyModel):
    """The one final decision on a submitted inspection (PRD 6.1)."""

    inspection = models.OneToOneField(Inspection, on_delete=models.PROTECT, related_name="decision")
    decision = models.CharField(max_length=12, choices=Decision.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inspection_decisions"
    )
    decided_at = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-decided_at", "-id"]
        default_permissions = ("view",)
        permissions = [
            ("approve_inspection", "Can approve inspections"),
            ("issue_concession", "Can issue concessions (Quality Manager / authorised supervisor)"),
            ("quarantine_lot", "Can quarantine a lot"),
        ]

    def __str__(self):
        return f"{self.get_decision_display()} - inspection #{self.inspection_id}"


class Concession(InsertOnlyModel):
    """Permanent quality-history flag for a lot released with a non-CTQ deviation (PRD 6.1)."""

    decision = models.OneToOneField(SupervisorDecision, on_delete=models.PROTECT, related_name="concession")
    reason_code = models.CharField(max_length=20, choices=ReasonCode.choices)
    justification = models.TextField("Engineering justification")
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="concessions_issued"
    )
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)

    def __str__(self):
        return f"Concession ({self.get_reason_code_display()})"


class QuarantineRecord(models.Model):
    """A lot held in the Digital Rejection Holding Area until MRB disposition (PRD 6.1)."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open (awaiting MRB)"
        CLOSED = "CLOSED", "Closed"

    traveler = models.ForeignKey(JobTraveler, on_delete=models.PROTECT, related_name="quarantines")
    inspection = models.OneToOneField(Inspection, on_delete=models.PROTECT, related_name="quarantine")
    location = models.CharField(max_length=60, default=HOLDING_AREA, editable=False)
    reason = models.TextField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="quarantines_created"
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = InsertOnlyQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        default_permissions = ("view",)

    def __str__(self):
        return f"Quarantine of {self.traveler} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if stored != self.Status.OPEN or self.status != self.Status.CLOSED:
                raise RecordLocked("A quarantine record can only be closed, once, by the MRB.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked("A quarantine record cannot be deleted.")


class Disposition(models.TextChoices):
    REWORK = "REWORK", "Rework order"
    SCRAP = "SCRAP", "Scrap"
    RTV = "RTV", "Return to vendor"
    CUSTOMER_CONCESSION = "CUSTOMER_CONCESSION", "Customer special concession"


_CORE_FIELDS = (
    "quarantine_id", "disposition", "notes", "rework_from_op", "scrap_cost_code",
    "scrap_bin_verified", "debit_note_ref", "proposed_by_id", "proposed_at",
)


class MRBDisposition(models.Model):
    """The Material Review Board's decision on a quarantined lot (PRD 6.3).

    Proposed by one MRB member, then signed by the Plant Manager, the Quality
    Head and the Production Lead. Only then is it executed.
    """

    quarantine = models.OneToOneField(QuarantineRecord, on_delete=models.PROTECT, related_name="disposition")
    disposition = models.CharField(max_length=20, choices=Disposition.choices)
    notes = models.TextField(help_text="Corrective action, reason or instructions.")
    rework_from_op = models.PositiveIntegerField(null=True, blank=True, help_text="Rework restarts at this op number.")
    scrap_cost_code = models.CharField(max_length=40, blank=True)
    scrap_bin_verified = models.BooleanField(default=False, help_text="Physically verified at the scrap bin station.")
    debit_note_ref = models.CharField(max_length=60, blank=True)
    customer_approval_file = models.FileField(upload_to="customer_approvals/%Y/%m/", blank=True)
    rework_traveler = models.OneToOneField(
        JobTraveler, on_delete=models.PROTECT, null=True, blank=True, related_name="rework_disposition"
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dispositions_proposed"
    )
    proposed_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True, help_text="When the disposition was executed.")

    objects = InsertOnlyQuerySet.as_manager()

    class Meta:
        default_permissions = ("view",)
        permissions = [
            ("propose_disposition", "Can propose an MRB disposition and upload customer approval"),
            ("sign_disposition", "Can sign off an MRB disposition"),
        ]

    def __str__(self):
        return f"{self.get_disposition_display()} for {self.quarantine.traveler}"

    @property
    def is_executed(self):
        return self.decided_at is not None

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.get(pk=self.pk)
            if stored.decided_at is not None:
                raise RecordLocked("An executed MRB disposition cannot be changed.")
            for name in _CORE_FIELDS:
                if getattr(stored, name) != getattr(self, name):
                    raise RecordLocked("The proposed disposition cannot be edited; sign it or leave it.")
            if stored.customer_approval_file and stored.customer_approval_file.name != self.customer_approval_file.name:
                raise RecordLocked("The customer approval document cannot be replaced.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked("An MRB disposition cannot be deleted.")


class MRBSignoff(InsertOnlyModel):
    """One Board member's signature on a disposition (PRD 6.3)."""

    disposition = models.ForeignKey(MRBDisposition, on_delete=models.PROTECT, related_name="signoffs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="mrb_signoffs")
    role = models.CharField(max_length=30, choices=Role.choices)
    signed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["signed_at", "id"]
        default_permissions = ("view",)
        constraints = [
            models.UniqueConstraint(fields=["disposition", "role"], name="one_signoff_per_role"),
            models.UniqueConstraint(fields=["disposition", "user"], name="one_signoff_per_user"),
        ]

    def __str__(self):
        return f"{self.get_role_display()} signed {self.disposition_id}"
