from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.core.audit import log_event
from apps.core.models import AuditLog

from .forms import FactoryUserChangeForm, FactoryUserCreationForm
from .models import User
from .services import sync_user_group

# Role changes are security-relevant, so they are written to the audit log.
AUDITED_FIELDS = ("role", "can_grant_concession", "is_active", "employee_id")
FACTORY_FIELDS = ("employee_id", "role", "can_grant_concession")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = FactoryUserChangeForm
    add_form = FactoryUserCreationForm
    fieldsets = DjangoUserAdmin.fieldsets + (("Factory role", {"fields": FACTORY_FIELDS}),)
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Factory role", {"fields": FACTORY_FIELDS}),
    )
    list_display = ("username", "employee_id", "get_full_name", "role", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("username", "employee_id", "first_name", "last_name")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            log_event(
                action=AuditLog.Action.CREATE,
                user=request.user,
                instance=obj,
                new_value=f"role={obj.role}",
            )
            return
        for field in form.changed_data:
            if field in AUDITED_FIELDS:
                log_event(
                    action=AuditLog.Action.UPDATE,
                    user=request.user,
                    instance=obj,
                    field_name=field,
                    previous_value=form.initial.get(field),
                    new_value=getattr(obj, field),
                )

    def save_related(self, request, form, formsets, change):
        # The admin form saves the "Groups" box AFTER save_model, which would
        # undo the role -> group sync done in User.save(). Re-apply it here.
        super().save_related(request, form, formsets, change)
        sync_user_group(form.instance)
