from django.contrib import admin

from .audit import log_event
from .models import AuditLog


def _plain(value):
    """Audit-friendly text: model instances become their primary key."""
    if hasattr(value, "pk"):
        return value.pk
    return value


class AuditedModelAdmin(admin.ModelAdmin):
    """Writes every create / change / delete made in the admin to the AuditLog
    (PRD 13.3), including rows edited through inlines."""

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            log_event(
                action=AuditLog.Action.CREATE, user=request.user, instance=obj,
                new_value=str(obj),
            )
            return
        for field in form.changed_data:
            log_event(
                action=AuditLog.Action.UPDATE, user=request.user, instance=obj,
                field_name=field,
                previous_value=_plain(form.initial.get(field)),
                new_value=_plain(form.cleaned_data.get(field)),
            )

    def delete_model(self, request, obj):
        log_event(
            action=AuditLog.Action.DELETE, user=request.user, instance=obj,
            previous_value=str(obj),
        )
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            log_event(
                action=AuditLog.Action.DELETE, user=request.user, instance=obj,
                previous_value=str(obj),
            )
        super().delete_queryset(request, queryset)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        changed_forms = {
            f.instance.pk: f
            for f in formset.forms
            if f.instance.pk and f.has_changed() and f not in formset.deleted_forms
        }
        for obj in formset.deleted_objects:
            log_event(
                action=AuditLog.Action.DELETE, user=request.user, instance=obj,
                previous_value=str(obj),
            )
            obj.delete()
        for obj in instances:
            is_new = obj._state.adding
            obj.save()
            if is_new:
                log_event(
                    action=AuditLog.Action.CREATE, user=request.user, instance=obj,
                    new_value=str(obj),
                )
                continue
            inline_form = changed_forms.get(obj.pk)
            for field in inline_form.changed_data if inline_form else ():
                log_event(
                    action=AuditLog.Action.UPDATE, user=request.user, instance=obj,
                    field_name=field,
                    previous_value=_plain(inline_form.initial.get(field)),
                    new_value=_plain(inline_form.cleaned_data.get(field)),
                )
        formset.save_m2m()


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Visible in the admin, never addable, editable or deletable.

    For records that only services may create (travelers, runs, serials...).
    """

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
