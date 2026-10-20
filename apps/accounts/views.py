from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.views.generic import ListView, TemplateView

from .models import User


class HomeView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/home.html"

    def get_context_data(self, **kwargs):
        from apps.core import dashboard

        ctx = super().get_context_data(**kwargs)
        ctx["dash"] = dashboard.build(self.request.user)
        return ctx


class UserListView(PermissionRequiredMixin, ListView):
    permission_required = "accounts.view_user"
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    ordering = ["employee_id"]
