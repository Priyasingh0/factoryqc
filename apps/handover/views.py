from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.core.exceptions import BusinessRuleViolation
from apps.plant.models import Machine
from apps.production.models import JobTraveler

from . import monitor, services
from .forms import HandoverForm, RemarksForm
from .models import ShiftHandover

VIEW_PERM = "handover.view_shifthandover"


class BoardView(PermissionRequiredMixin, TemplateView):
    permission_required = VIEW_PERM
    template_name = "handover/board.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        rows = []
        for machine in Machine.objects.filter(is_active=True).select_related("department"):
            rows.append({
                "machine": machine,
                "problem": services.machine_problem(machine, now),
                "missing": services.missing_handover(machine, now),
                "latest": machine.shift_handovers.first(),
            })
        user = self.request.user
        context.update(
            rows=rows, pending=services.pending_handovers(),
            recent=ShiftHandover.objects.select_related("machine", "outgoing_by")[:15],
            can_create=user.has_perm(services.PERM_CREATE),
        )
        return context


class NewHandoverView(PermissionRequiredMixin, View):
    permission_required = services.PERM_CREATE

    def _render(self, request, machine, form):
        return render(request, "handover/new.html", {
            "machine": machine, "form": form, "snapshot": services.machine_snapshot(machine),
        })

    def get(self, request, pk):
        return self._render(request, get_object_or_404(Machine, pk=pk), HandoverForm())

    def post(self, request, pk):
        machine = get_object_or_404(Machine, pk=pk)
        form = HandoverForm(request.POST)
        if form.is_valid():
            try:
                handover = services.create_handover(machine, request.user, **form.cleaned_data)
            except BusinessRuleViolation as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Handover saved. A supervisor must countersign it.")
                return redirect("handover:detail", pk=handover.pk)
        return self._render(request, machine, form)


class DetailView(PermissionRequiredMixin, TemplateView):
    permission_required = VIEW_PERM
    template_name = "handover/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        handover = get_object_or_404(
            ShiftHandover.objects.select_related("machine", "outgoing_by"), pk=self.kwargs["pk"]
        )
        user = self.request.user
        signed = hasattr(handover, "signoff")
        acked = hasattr(handover, "acknowledgement")
        context.update(
            handover=handover, form=RemarksForm(),
            signoff=handover.signoff if signed else None,
            acknowledgement=handover.acknowledgement if acked else None,
            can_sign=not signed and user.has_perm(services.PERM_SIGN) and handover.outgoing_by_id != user.pk,
            can_ack=signed and not acked and user.has_perm(services.PERM_ACK)
            and user.pk not in (handover.outgoing_by_id, handover.signoff.signed_by_id),
        )
        return context


class ActionView(PermissionRequiredMixin, View):
    """POST-only buttons on the handover page; the rules are in services."""

    http_method_names = ["post"]

    def post(self, request, pk):
        handover = get_object_or_404(ShiftHandover, pk=pk)
        form = RemarksForm(request.POST)
        remarks = form.cleaned_data["remarks"] if form.is_valid() else ""
        try:
            self.act(handover, request.user, remarks)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, self.success_message)
        return redirect("handover:detail", pk=pk)


class SignView(ActionView):
    permission_required = services.PERM_SIGN
    success_message = "Handover countersigned. The incoming shift can now acknowledge it."

    def act(self, handover, user, remarks):
        services.sign_handover(handover, user, remarks)


class AcknowledgeView(ActionView):
    permission_required = services.PERM_ACK
    success_message = "Handover acknowledged. The machine is unlocked for this shift."

    def act(self, handover, user, remarks):
        services.acknowledge_handover(handover, user, remarks)


class FlowListView(PermissionRequiredMixin, TemplateView):
    permission_required = "production.view_jobtraveler"
    template_name = "handover/flow_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        travelers = JobTraveler.objects.select_related("work_order__part").order_by("-created_at", "-id")[:40]
        context["flows"] = [services.flow_status(t) for t in travelers]
        return context


class FlowDetailView(PermissionRequiredMixin, TemplateView):
    permission_required = "production.view_jobtraveler"
    template_name = "handover/flow_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        traveler = get_object_or_404(JobTraveler, pk=self.kwargs["pk"])
        context["flow"] = services.flow_status(traveler)
        return context


class MonitorView(PermissionRequiredMixin, TemplateView):
    """Live bottleneck and queue monitor (PRD 8.3)."""

    permission_required = "production.view_jobtraveler"
    template_name = "handover/monitor.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(monitor.floor_report(), queue_limit_hours=int(monitor.QUEUE_LIMIT.total_seconds() // 3600))
        return context
