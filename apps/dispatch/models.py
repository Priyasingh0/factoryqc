from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.signing import QRKind, make_token
from apps.production.models import JobTraveler, PartSerial
from apps.review.exceptions import RecordLocked
from apps.review.models import InsertOnlyModel



class ManifestQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise RecordLocked("Shipment manifests cannot be updated in bulk.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise RecordLocked("Shipment manifests cannot be updated in bulk.")

    def delete(self):
        raise RecordLocked("Shipment manifests cannot be deleted.")


class ShipmentManifest(models.Model):
    """Dispatch manifest for one shipment of a finished lot (PRD 11).

    Logs the customer PO, batch size, vehicle and the serial numbers shipped. Once
    created only the status can move, OPEN to DISPATCHED, when the gate pass is issued.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        DISPATCHED = "DISPATCHED", "Dispatched (gate pass issued)"

    traveler = models.ForeignKey(JobTraveler, on_delete=models.PROTECT, related_name="manifests")
    customer_po = models.CharField("Customer PO", max_length=50)
    vehicle_number = models.CharField("Transport vehicle", max_length=30)
    batch_size = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="manifests_created"
    )
    created_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)

    objects = ManifestQuerySet.as_manager()

    _CORE_FIELDS = ("traveler_id", "customer_po", "vehicle_number", "batch_size", "created_by_id", "created_at")

    class Meta:
        ordering = ["-created_at", "-id"]
        default_permissions = ("view",)
        permissions = [
            ("create_manifest", "Can create a shipment manifest (after the shipping interlock is green)"),
            ("issue_gate_pass", "Can issue the dispatch gate pass"),
            ("issue_shipping_documents", "Can issue packing slips, commercial invoices and QR labels"),
        ]

    def __str__(self):
        return f"Manifest {self.pk} ({self.traveler.work_order.number})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).first()
            if stored is None:
                raise RecordLocked("This manifest no longer exists.")
            changed = [f for f in self._CORE_FIELDS if getattr(stored, f) != getattr(self, f)]
            if changed:
                raise RecordLocked("A manifest cannot be edited after it is created.")
            if not (stored.status == self.Status.OPEN and self.status == self.Status.DISPATCHED):
                raise RecordLocked("A manifest can only move from Open to Dispatched, once.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RecordLocked("A shipment manifest cannot be deleted.")

    @property
    def is_dispatched(self):
        return self.status == self.Status.DISPATCHED


class ManifestSerial(InsertOnlyModel):
    """One part on a manifest. A serial can ship only once (PRD 7.2 forward traceability)."""

    manifest = models.ForeignKey(ShipmentManifest, on_delete=models.PROTECT, related_name="items")
    serial = models.OneToOneField(PartSerial, on_delete=models.PROTECT, related_name="manifest_item")

    class Meta:
        default_permissions = ("view",)
        ordering = ["serial__serial_number"]

    def __str__(self):
        return f"{self.serial} on manifest {self.manifest_id}"

    def save(self, *args, **kwargs):
        if self._state.adding:
            stored = ShipmentManifest.objects.filter(pk=self.manifest_id).values_list("status", flat=True).first()
            if stored != ShipmentManifest.Status.OPEN:
                raise RecordLocked("Parts cannot be added to a manifest that has been dispatched.")
            if self.serial.traveler_id != self.manifest.traveler_id:
                raise RecordLocked("That serial number belongs to a different lot.")
        super().save(*args, **kwargs)


class GatePass(InsertOnlyModel):
    """The dispatch gate pass; exists only if the shipping interlock was green (PRD 10.1)."""

    manifest = models.OneToOneField(ShipmentManifest, on_delete=models.PROTECT, related_name="gate_pass")
    number = models.CharField(max_length=20, unique=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="gate_passes")
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)
        ordering = ["-issued_at", "-id"]

    def __str__(self):
        return self.number


class ShippingDocument(InsertOnlyModel):
    """Packing slip or commercial invoice, issued once per manifest (PRD 10.2)."""

    class Kind(models.TextChoices):
        PACKING_SLIP = "PACKING_SLIP", "Packing slip"
        INVOICE = "INVOICE", "Commercial invoice"

    manifest = models.ForeignKey(ShipmentManifest, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=14, choices=Kind.choices)
    number = models.CharField(max_length=20, unique=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shipping_documents"
    )
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)
        ordering = ["issued_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["manifest", "kind"], name="one_document_of_each_kind_per_manifest"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"


class ShipmentLabel(InsertOnlyModel):
    """Customer verification QR label for one crate (PRD 11)."""

    manifest = models.ForeignKey(ShipmentManifest, on_delete=models.PROTECT, related_name="labels")
    crate_number = models.PositiveIntegerField()
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="labels_issued")
    issued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ("view",)
        ordering = ["manifest_id", "crate_number"]
        constraints = [
            models.UniqueConstraint(fields=["manifest", "crate_number"], name="unique_crate_number_per_manifest"),
        ]

    def __str__(self):
        return f"Label {self.manifest_id}/{self.crate_number}"

    @property
    def token(self):
        """Signed token printed as the crate QR code (PRD 13.1)."""
        return make_token(QRKind.SHIPMENT, self.pk) if self.pk else ""
