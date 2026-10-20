from django.contrib import admin

from apps.core.admin_mixins import AuditedModelAdmin, ReadOnlyModelAdmin

from .models import (
    JobTraveler,
    PartSerial,
    ProductionRun,
    RoutingOperation,
    TravelerOperation,
    WorkOrder,
)


@admin.register(WorkOrder)
class WorkOrderAdmin(AuditedModelAdmin):
    """Create work orders here; release them from the Work orders page."""

    list_display = ("number", "part", "quantity", "customer_po", "heat_number", "released_at")
    list_filter = ("part",)
    search_fields = ("number", "customer_po", "heat_number")
    readonly_fields = ("created_by", "released_by", "released_at")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_released:
            return [f.name for f in self.model._meta.fields]
        return self.readonly_fields

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.is_released:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_released:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(RoutingOperation)
class RoutingOperationAdmin(AuditedModelAdmin):
    list_display = (
        "part", "op_number", "name", "hold_minutes", "requires_first_piece", "is_final_pdi",
    )
    list_filter = ("part", "requires_first_piece", "is_final_pdi")
    search_fields = ("part__part_number", "name")

    def _in_use(self, obj):
        return obj is not None and obj.traveler_operations.exists()

    def get_readonly_fields(self, request, obj=None):
        if self._in_use(obj):
            return [f.name for f in self.model._meta.fields]
        return ()

    def has_change_permission(self, request, obj=None):
        return not self._in_use(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._in_use(obj) and super().has_delete_permission(request, obj)


@admin.register(JobTraveler)
class JobTravelerAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "work_order", "quantity", "status", "is_rework")
    list_filter = ("status", "is_rework")


@admin.register(TravelerOperation)
class TravelerOperationAdmin(ReadOnlyModelAdmin):
    list_display = ("traveler", "routing_operation", "status", "machine", "operator", "clock_in", "clock_out")
    list_filter = ("status",)


@admin.register(ProductionRun)
class ProductionRunAdmin(ReadOnlyModelAdmin):
    list_display = ("traveler_operation", "state", "parts_produced", "next_check_due_at")


@admin.register(PartSerial)
class PartSerialAdmin(ReadOnlyModelAdmin):
    list_display = ("serial_number", "traveler", "created_at")
    search_fields = ("serial_number",)
