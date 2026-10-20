from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, ListView

from apps.core.exceptions import BusinessRuleViolation

from . import services
from .forms import PMCheckForm
from .models import Gauge, Machine, Part, Shift


class MachineListView(PermissionRequiredMixin, ListView):
    permission_required = "plant.view_machine"
    model = Machine
    template_name = "plant/machine_list.html"

    def get_queryset(self):
        return super().get_queryset().select_related("department")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rows"] = services.readiness_for_machines(context["object_list"])
        return context


class MachineDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "plant.view_machine"
    model = Machine
    template_name = "plant/machine_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        shift_date, shift = services.shift_for()
        context.update(
            readiness=services.machine_readiness(self.object),
            shift_date=shift_date,
            shift_label=Shift(shift).label,
            recent_checks=self.object.pm_checks.select_related("performed_by")[:10],
            form=PMCheckForm(),
            can_record=self.request.user.has_perm("plant.add_machinepmcheck"),
        )
        return context


class PMCheckCreateView(PermissionRequiredMixin, View):
    permission_required = "plant.add_machinepmcheck"
    http_method_names = ["post"]

    def post(self, request, pk):
        machine = get_object_or_404(Machine, pk=pk)
        form = PMCheckForm(request.POST)
        if form.is_valid():
            try:
                services.record_pm_check(
                    machine,
                    request.user,
                    completed=form.cleaned_data["completed"],
                    safety_zero_point_ok=form.cleaned_data["safety_zero_point_ok"],
                    remarks=form.cleaned_data["remarks"],
                )
            except BusinessRuleViolation as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"PM check saved for {machine.code}.")
        else:
            messages.error(request, "Invalid PM check form.")
        return redirect("plant:machine_detail", pk=machine.pk)


class GaugeListView(PermissionRequiredMixin, ListView):
    permission_required = "plant.view_gauge"
    model = Gauge
    template_name = "plant/gauge_list.html"


class PartListView(PermissionRequiredMixin, ListView):
    permission_required = "plant.view_part"
    model = Part
    template_name = "plant/part_list.html"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("programs")
