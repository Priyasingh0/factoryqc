from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyModelAdmin

from .models import DefectPhoto, Inspection, Measurement


@admin.register(Inspection)
class InspectionAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "checklist_type", "traveler", "inspector", "status", "started_at")
    list_filter = ("status", "checklist_type")


@admin.register(Measurement)
class MeasurementAdmin(ReadOnlyModelAdmin):
    list_display = ("inspection", "field", "value_numeric", "value_bool", "gauge", "is_within_tolerance")
    list_filter = ("is_within_tolerance",)


@admin.register(DefectPhoto)
class DefectPhotoAdmin(ReadOnlyModelAdmin):
    list_display = ("measurement", "uploaded_by", "uploaded_at")
