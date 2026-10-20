from django.contrib import admin

from apps.core.admin_mixins import AuditedModelAdmin

from .models import BlueprintPin, ChecklistTemplate, TemplateField, TemplateStatus


class DraftOnlyInline(admin.TabularInline):
    """Inline rows can only be added, changed or removed while the template is a Draft."""

    extra = 0

    def _draft(self, obj):
        return obj is None or obj.status == TemplateStatus.DRAFT

    def has_add_permission(self, request, obj=None):
        return self._draft(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._draft(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._draft(obj) and super().has_delete_permission(request, obj)


class BlueprintPinInline(DraftOnlyInline):
    model = BlueprintPin


class TemplateFieldInline(DraftOnlyInline):
    model = TemplateField
    fields = (
        "sequence", "label", "pin", "field_type", "nominal", "upper_tol", "lower_tol",
        "max_limit", "min_limit", "unit", "is_ctq", "required_gauge_type",
    )


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(AuditedModelAdmin):
    """Authoring screen. Status changes happen only through the workflow pages."""

    list_display = ("part", "checklist_type", "operation_number", "revision", "status", "created_by")
    list_filter = ("status", "checklist_type")
    search_fields = ("part__part_number", "part__name")
    inlines = [BlueprintPinInline, TemplateFieldInline]
    workflow_fields = ("status", "review_notes", "created_by", "approved_by", "approved_at")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.status != TemplateStatus.DRAFT:
            return [f.name for f in self.model._meta.fields]
        return self.workflow_fields

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.status != TemplateStatus.DRAFT:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != TemplateStatus.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
