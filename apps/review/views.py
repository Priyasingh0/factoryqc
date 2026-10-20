from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.core.exceptions import BusinessRuleViolation
from apps.inspections.models import Inspection, InspectionStatus
from apps.plant.services import shift_for

from . import services
from .forms import (
    ApproveForm, ConcessionForm, CustomerApprovalForm, DispositionForm, QuarantineForm,
)
from .models import MRB_ROLES, Disposition, MRBDisposition, QuarantineRecord

VIEW_PERM = "review.view_supervisordecision"


class QueueView(PermissionRequiredMixin, TemplateView):
    """PRD 6.1: live approval queue, sortable by shift, machine or urgency."""

    permission_required = VIEW_PERM
    template_name = "review/queue.html"
    SORTS = {"urgency": "Waiting longest", "shift": "Shift", "machine": "Machine"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sort = self.request.GET.get("sort", "urgency")
        if sort not in self.SORTS:
            sort = "urgency"
        rows = []
        for i in Inspection.objects.filter(status__in=services.DECIDABLE).select_related(
            "traveler__work_order__part", "machine", "inspector", "template"
        ):
            failed = list(i.measurements.filter(is_within_tolerance=False).select_related("field"))
            waiting_since = i.frozen_at or i.submitted_at or i.started_at
            rows.append({
                "inspection": i,
                "shift_date": shift_for(i.started_at)[0],
                "shift": shift_for(i.started_at)[1],
                "failed": len(failed),
                "ctq": any(m.field.is_ctq for m in failed),
                "waiting_since": waiting_since,
                "waiting_minutes": max(0, int((timezone.now() - waiting_since).total_seconds() // 60)),
            })
        if sort == "urgency":  # CTQ failures first, then who has waited longest
            rows.sort(key=lambda r: (not r["ctq"], r["waiting_since"]))
        elif sort == "shift":
            rows.sort(key=lambda r: (r["shift_date"], r["shift"], r["waiting_since"]))
        else:
            rows.sort(key=lambda r: (r["inspection"].machine.code, r["waiting_since"]))
        context.update(rows=rows, sort=sort, sorts=self.SORTS)
        return context


class ReviewDetailView(PermissionRequiredMixin, DetailView):
    permission_required = VIEW_PERM
    model = Inspection
    template_name = "review/review_detail.html"
    context_object_name = "inspection"

    def get_queryset(self):
        return super().get_queryset().select_related(
            "template", "traveler__work_order__part", "machine", "inspector"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        inspection, user = self.object, self.request.user
        readings = [
            {"m": m, "photos": list(m.photos.all())}
            for m in inspection.measurements.select_related("field", "gauge").prefetch_related("photos")
        ]
        problems = services.decision_problems(inspection)
        context.update(
            readings=readings,
            problems=problems,
            can_approve=user.has_perm(services.PERM_APPROVE) and not problems["approve"],
            can_concede=user.has_perm(services.PERM_CONCESSION) and user.may_issue_concession and not problems["concession"],
            may_concede_role=user.has_perm(services.PERM_CONCESSION) and user.may_issue_concession,
            can_quarantine=user.has_perm(services.PERM_QUARANTINE) and not problems["quarantine"],
            approve_form=ApproveForm(),
            concession_form=ConcessionForm(),
            quarantine_form=QuarantineForm(),
            decision=getattr(inspection, "decision", None),
            quarantine=getattr(inspection, "quarantine", None),
        )
        return context


class DecisionView(PermissionRequiredMixin, View):
    http_method_names = ["post"]
    success_message = ""

    def decide(self, request, inspection):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, pk):
        inspection = get_object_or_404(Inspection, pk=pk)
        try:
            result = self.decide(request, inspection)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
            return redirect("review:detail", pk=pk)
        messages.success(request, self.success_message)
        if isinstance(result, QuarantineRecord):
            return redirect("review:quarantine_detail", pk=result.pk)
        return redirect("review:detail", pk=pk)


class ApproveView(DecisionView):
    permission_required = services.PERM_APPROVE
    success_message = "Inspection approved. The lot is cleared for the next step."

    def decide(self, request, inspection):
        form = ApproveForm(request.POST)
        return services.approve_inspection(
            inspection, request.user, form.cleaned_data["remarks"] if form.is_valid() else ""
        )


class ConcessionView(DecisionView):
    permission_required = services.PERM_CONCESSION
    success_message = "Concession issued. The lot is permanently flagged in quality history."

    def decide(self, request, inspection):
        form = ConcessionForm(request.POST)
        if not form.is_valid():
            raise services.DecisionBlocked("Select a reason code and write the justification.")
        return services.issue_concession(
            inspection, request.user, form.cleaned_data["reason_code"], form.cleaned_data["justification"]
        )


class QuarantineView(DecisionView):
    permission_required = services.PERM_QUARANTINE
    success_message = "Lot quarantined and moved to the Digital Rejection Holding Area."

    def decide(self, request, inspection):
        form = QuarantineForm(request.POST)
        if not form.is_valid():
            raise services.DecisionBlocked("Explain why the lot is rejected.")
        return services.quarantine_lot(inspection, request.user, form.cleaned_data["reason"])


class QuarantineListView(PermissionRequiredMixin, ListView):
    permission_required = VIEW_PERM
    model = QuarantineRecord
    template_name = "review/quarantine_list.html"

    def get_queryset(self):
        return super().get_queryset().select_related("traveler__work_order__part", "inspection")


class QuarantineDetailView(PermissionRequiredMixin, DetailView):
    permission_required = VIEW_PERM
    model = QuarantineRecord
    template_name = "review/quarantine_detail.html"
    context_object_name = "quarantine"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quarantine, user = self.object, self.request.user
        disposition = getattr(quarantine, "disposition", None)
        open_ = quarantine.status == QuarantineRecord.Status.OPEN
        signed_roles = set(disposition.signoffs.values_list("role", flat=True)) if disposition else set()
        context.update(
            disposition=disposition,
            can_propose=open_ and disposition is None and user.has_perm(services.PERM_PROPOSE),
            disposition_form=DispositionForm(),
            can_sign=(
                disposition is not None and not disposition.is_executed and user.has_perm(services.PERM_SIGN)
                and user.role in MRB_ROLES and user.role not in signed_roles
            ),
            can_upload=(
                disposition is not None and not disposition.is_executed
                and disposition.disposition == Disposition.CUSTOMER_CONCESSION
                and not disposition.customer_approval_file and user.has_perm(services.PERM_PROPOSE)
            ),
            approval_form=CustomerApprovalForm(),
            blockers=services.execution_blockers(disposition) if disposition and not disposition.is_executed else [],
            board=[(role.label, role.value in signed_roles) for role in MRB_ROLES],
            signoffs=disposition.signoffs.select_related("user") if disposition else [],
            routing=quarantine.traveler.work_order.part.routing.filter(is_final_pdi=False),
        )
        return context


class DispositionActionView(PermissionRequiredMixin, View):
    http_method_names = ["post"]


class ProposeView(DispositionActionView):
    permission_required = services.PERM_PROPOSE

    def post(self, request, pk):
        quarantine = get_object_or_404(QuarantineRecord, pk=pk)
        form = DispositionForm(request.POST)
        try:
            if not form.is_valid():
                raise services.InvalidDisposition("Check the disposition details.")
            data = form.cleaned_data
            services.propose_disposition(
                quarantine, request.user, data["disposition"], data["notes"],
                rework_from_op=data["rework_from_op"], scrap_cost_code=data["scrap_cost_code"],
                scrap_bin_verified=data["scrap_bin_verified"], debit_note_ref=data["debit_note_ref"],
            )
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Disposition proposed. The Plant Manager, Quality Head and Production Lead must sign.")
        return redirect("review:quarantine_detail", pk=pk)


class SignView(DispositionActionView):
    permission_required = services.PERM_SIGN

    def post(self, request, pk):
        disposition = get_object_or_404(MRBDisposition, pk=pk)
        try:
            executed = services.sign_disposition(disposition, request.user)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                "Signed. All three signatures are in: the disposition has been executed." if executed
                else "Signed. Waiting for the rest of the Board.",
            )
        return redirect("review:quarantine_detail", pk=disposition.quarantine_id)


class CustomerApprovalView(DispositionActionView):
    permission_required = services.PERM_PROPOSE

    def post(self, request, pk):
        disposition = get_object_or_404(MRBDisposition, pk=pk)
        try:
            executed = services.attach_customer_approval(disposition, request.FILES.get("document"), request.user)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                "Approval stored and the lot released." if executed
                else "Approval stored. Waiting for the Board's signatures.",
            )
        return redirect("review:quarantine_detail", pk=disposition.quarantine_id)


class CustomerApprovalFileView(PermissionRequiredMixin, View):
    permission_required = VIEW_PERM

    def get(self, request, pk):
        disposition = get_object_or_404(MRBDisposition, pk=pk)
        if not disposition.customer_approval_file:
            raise Http404("No document.")
        kind = disposition.customer_approval_file.name.rsplit(".", 1)[-1].lower()
        content_type = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png"}.get(kind)
        if content_type is None:
            raise Http404("Unsupported file.")
        response = FileResponse(disposition.customer_approval_file.open("rb"), content_type=content_type)
        response["X-Content-Type-Options"] = "nosniff"
        return response
