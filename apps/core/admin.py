from django.contrib import admin

from .models import AuditLog, Notification


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Visible in the admin but never addable, editable or deletable."""

    list_display = ("timestamp", "user_label", "action", "model_name", "object_id", "ip_address")
    list_filter = ("action",)
    search_fields = ("user_label", "model_name", "object_id", "remarks")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "subject", "recipient", "status")
    list_filter = ("channel", "status")
