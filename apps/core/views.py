from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.generic import ListView

from .context_processors import SEEN_KEY
from .models import AuditLog, Notification


class AuditLogListView(PermissionRequiredMixin, ListView):
    permission_required = "core.view_auditlog"
    model = AuditLog
    template_name = "core/audit_log_list.html"
    context_object_name = "entries"
    paginate_by = 50

    def get_queryset(self):
        return super().get_queryset().select_related("user")


class NotificationListView(LoginRequiredMixin, ListView):
    """The signed-in user's own notifications. Opening the page marks everything as seen."""

    template_name = "core/notifications.html"
    context_object_name = "notifications"
    paginate_by = 25

    def get_queryset(self):
        qs = Notification.objects.filter(recipient=self.request.user)
        if self.request.GET.get("channel") in Notification.Channel.values:
            qs = qs.filter(channel=self.request.GET["channel"])
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        seen = self.request.session.get(SEEN_KEY)
        ctx["seen_before"] = parse_datetime(seen) if seen else None
        ctx["channels"] = Notification.Channel.choices
        self.request.session[SEEN_KEY] = timezone.now().isoformat()
        return ctx
