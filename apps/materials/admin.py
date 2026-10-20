from django.contrib import admin

from apps.core.admin_mixins import AuditedModelAdmin, ReadOnlyModelAdmin

from .models import SNCR, GradeProperty, LabResult, MaterialGrade, MaterialIssue, MTCReading, RawMaterialIntake, Supplier


@admin.register(Supplier)
class SupplierAdmin(AuditedModelAdmin):
    list_display = ("code", "name", "email", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


class GradePropertyInline(admin.TabularInline):
    model = GradeProperty
    extra = 0


@admin.register(MaterialGrade)
class MaterialGradeAdmin(AuditedModelAdmin):
    list_display = ("code", "description", "requires_lab_testing", "is_active")
    list_filter = ("requires_lab_testing", "is_active")
    search_fields = ("code", "description")
    inlines = [GradePropertyInline]


@admin.register(RawMaterialIntake)
class RawMaterialIntakeAdmin(ReadOnlyModelAdmin):
    list_display = ("lot_number", "supplier", "grade", "heat_number", "quantity", "status", "received_at")
    list_filter = ("status", "grade")
    search_fields = ("heat_number", "po_number")


@admin.register(MTCReading)
class MTCReadingAdmin(ReadOnlyModelAdmin):
    list_display = ("intake", "grade_property", "value", "is_within_spec")


@admin.register(LabResult)
class LabResultAdmin(ReadOnlyModelAdmin):
    list_display = ("intake", "passed", "report_reference", "recorded_by", "recorded_at")


@admin.register(SNCR)
class SNCRAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "intake", "reason_code", "status", "raised_at")
    list_filter = ("status", "reason_code")


@admin.register(MaterialIssue)
class MaterialIssueAdmin(ReadOnlyModelAdmin):
    list_display = ("intake", "work_order", "quantity", "issued_by", "issued_at")
