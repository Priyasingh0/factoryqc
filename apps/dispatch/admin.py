from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyModelAdmin

from .models import GatePass, ManifestSerial, ShipmentLabel, ShipmentManifest, ShippingDocument


@admin.register(ShipmentManifest)
class ShipmentManifestAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "traveler", "customer_po", "vehicle_number", "batch_size", "status", "created_at")
    list_filter = ("status",)


@admin.register(ManifestSerial)
class ManifestSerialAdmin(ReadOnlyModelAdmin):
    list_display = ("manifest", "serial")


@admin.register(GatePass)
class GatePassAdmin(ReadOnlyModelAdmin):
    list_display = ("number", "manifest", "issued_by", "issued_at")


@admin.register(ShippingDocument)
class ShippingDocumentAdmin(ReadOnlyModelAdmin):
    list_display = ("number", "kind", "manifest", "issued_by", "issued_at")
    list_filter = ("kind",)


@admin.register(ShipmentLabel)
class ShipmentLabelAdmin(ReadOnlyModelAdmin):
    list_display = ("manifest", "crate_number", "issued_by", "issued_at")
