from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.core.exceptions import BusinessRuleViolation
from apps.inspections.models import InspectionStatus
from apps.materials import services as materials

from . import services
from .forms import RecordPartsForm, StartOperationForm, TraceForm
from .models import JobTraveler, ProductionRun, TravelerOperation, WorkOrder

OP = TravelerOperation.Status


class WorkOrderListView(PermissionRequiredMixin, ListView):
    permission_required = "production.view_workorder"
    model = WorkOrder
    template_name = "production/work_order_list.html"

    def get_queryset(self):
        return super().get_queryset().select_related("part")


class WorkOrderDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "production.view_workorder"
    model = WorkOrder
    template_name = "production/work_order_detail.html"
    context_object_name = "wo"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        wo = self.object
        context.update(
            routing=wo.part.routing.all(),
            lots=wo.material_issues.select_related("intake__supplier"),
            travelers=wo.travelers.all(),
            problems=[] if wo.is_released else services.release_problems(wo),
            can_release=not wo.is_released and self.request.user.has_perm(services.PERM_RELEASE),
        )
        return context


class ReleaseWorkOrderView(PermissionRequiredMixin, View):
    permission_required = services.PERM_RELEASE
    http_method_names = ["post"]

    def post(self, request, pk):
        wo = get_object_or_404(WorkOrder, pk=pk)
        try:
            traveler = services.release_work_order(wo, request.user)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
            return redirect("production:work_order_detail", pk=wo.pk)
        messages.success(request, "Work order released. The job traveler has been generated.")
        return redirect("production:traveler_detail", pk=traveler.pk)


class TravelerListView(PermissionRequiredMixin, ListView):
    permission_required = "production.view_jobtraveler"
    model = JobTraveler
    template_name = "production/traveler_list.html"

    def get_queryset(self):
        return super().get_queryset().select_related("work_order__part")


class TravelerDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "production.view_jobtraveler"
    model = JobTraveler
    template_name = "production/traveler_detail.html"
    context_object_name = "traveler"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        traveler, user, now = self.object, self.request.user, timezone.now()
        can_operate = user.has_perm(services.PERM_OPERATE)
        can_clear = user.has_perm(services.PERM_CLEAR_FIRST_PIECE)
        can_check = user.has_perm(services.PERM_LOG_CHECK)
        next_op = services.next_pending_operation(traveler)
        active = traveler.status == JobTraveler.Status.ACTIVE

        rows = []
        for op in services.ordered_operations(traveler):
            run = getattr(op, "run", None)
            state = services.inspection_state(run, now) if run else None
            in_progress = op.status == OP.IN_PROGRESS and run is not None
            remaining = op.hold_remaining(now)
            rows.append({
                "op": op,
                "step": op.routing_operation,
                "run": run,
                "hold_text": services.format_remaining(remaining) if remaining else "",
                "inspection": state,
                "can_start": active and can_operate and op == next_op and not op.routing_operation.is_final_pdi,
                "can_count": active and can_operate and in_progress and run.state == ProductionRun.State.RUNNING,
                "can_complete": active and can_operate and in_progress and run.state == ProductionRun.State.RUNNING,
                "can_clear": (
                    active and can_clear and in_progress and run.state == ProductionRun.State.SETUP
                    and run.first_piece_inspection_id is not None and op.operator_id != user.pk
                ),
                "can_check": (
                    active and can_check and in_progress and run.state == ProductionRun.State.RUNNING
                    and op.routing_operation.has_periodic_inspection
                ),
            })
        hold = services.blocking_hold(traveler, now)
        context.update(
            concession=traveler.inspections.filter(status=InspectionStatus.CONCESSION).exists(),
            quarantine=traveler.quarantines.filter(status="OPEN").first(),
        )
        context.update(
            rows=rows,
            start_form=StartOperationForm(),
            parts_form=RecordPartsForm(),
            hold_banner=(
                f"Mandatory Cooling Hold Active ({services.format_remaining(hold[1])} remaining) "
                f"after {hold[0].routing_operation}."
            ) if hold else "",
            serials=traveler.serials.all(),
        )
        return context


class OperationActionView(PermissionRequiredMixin, View):
    """Base for the POST-only shopfloor buttons; rules live in services.py."""

    http_method_names = ["post"]
    success_message = ""

    def run(self, request, operation):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, pk):
        operation = get_object_or_404(TravelerOperation.objects.select_related("traveler"), pk=pk)
        try:
            self.run(request, operation)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, self.success_message)
        return redirect("production:traveler_detail", pk=operation.traveler_id)


class StartOperationView(OperationActionView):
    permission_required = services.PERM_OPERATE
    success_message = "Operation started."

    def run(self, request, operation):
        form = StartOperationForm(request.POST)
        if not form.is_valid():
            raise services.OperationBlocked("Scan both the station QR code and the traveler barcode.")
        services.scan_and_start(
            operation, request.user, form.cleaned_data["machine_qr"], form.cleaned_data["traveler_code"]
        )


class RecordPartsView(OperationActionView):
    permission_required = services.PERM_OPERATE
    success_message = "Parts recorded."

    def run(self, request, operation):
        form = RecordPartsForm(request.POST)
        if not form.is_valid():
            raise services.OperationBlocked("Enter a whole number of parts, at least 1.")
        services.record_parts(operation, request.user, form.cleaned_data["count"])


class CompleteOperationView(OperationActionView):
    permission_required = services.PERM_OPERATE
    success_message = "Operation signed off."

    def run(self, request, operation):
        services.complete_operation(operation, request.user)


class ClearFirstPieceView(OperationActionView):
    permission_required = services.PERM_CLEAR_FIRST_PIECE
    success_message = "First piece signed off. Production counters are unlocked."

    def run(self, request, operation):
        services.clear_first_piece(get_object_or_404(ProductionRun, traveler_operation=operation), request.user)


class PeriodicCheckView(OperationActionView):
    permission_required = services.PERM_LOG_CHECK
    success_message = "Periodic inspection logged."

    def run(self, request, operation):
        services.log_periodic_check(get_object_or_404(ProductionRun, traveler_operation=operation), request.user)


class TraceView(PermissionRequiredMixin, TemplateView):
    """Forward recall by heat number and backward birth certificate by serial (PRD 7.2)."""

    permission_required = "production.view_jobtraveler"
    template_name = "production/trace.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = TraceForm(self.request.GET or None)
        context["form"] = form
        if form.is_bound and form.is_valid():
            kind, q = form.cleaned_data["kind"], form.cleaned_data["q"]
            context["searched"] = True
            if kind == "heat":
                context["work_orders"] = services.forward_trace(q)
                context["lots"] = materials.lots_for_heat(q)
            else:
                context["birth"] = services.backward_trace(q)
        return context
