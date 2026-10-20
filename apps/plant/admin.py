from django.contrib import admin

from apps.core.admin_mixins import AuditedModelAdmin

from .models import Department, Gauge, Machine, MachinePMCheck, Part, ToolingProgram


@admin.register(Department)
class DepartmentAdmin(AuditedModelAdmin):
    list_display = ("code", "name")
    search_fields = ("code", "name")


@admin.register(Machine)
class MachineAdmin(AuditedModelAdmin):
    list_display = ("code", "name", "department", "is_active")
    list_filter = ("department", "is_active")
    search_fields = ("code", "name")
    readonly_fields = ("qr_token",)


class ToolingProgramInline(admin.TabularInline):
    model = ToolingProgram
    extra = 0


@admin.register(Part)
class PartAdmin(AuditedModelAdmin):
    list_display = ("part_number", "name")
    search_fields = ("part_number", "name")
    inlines = [ToolingProgramInline]


@admin.register(Gauge)
class GaugeAdmin(AuditedModelAdmin):
    list_display = ("gauge_id", "gauge_type", "calibration_expiry", "status", "is_active")
    list_filter = ("gauge_type", "is_active")
    search_fields = ("gauge_id", "description")


@admin.register(MachinePMCheck)
class MachinePMCheckAdmin(admin.ModelAdmin):
    """View-only: PM checks are recorded through the service so they are validated."""

    list_display = ("date", "shift", "machine", "completed", "safety_zero_point_ok", "performed_by")
    list_filter = ("shift", "completed", "safety_zero_point_ok")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
