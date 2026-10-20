from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.core.exceptions import BusinessRuleViolation
from apps.production.models import WorkOrder

from . import services
from .forms import IntakeForm, IssueForm, LabResultForm, RecallForm, RejectForm, ResolveForm
from .models import SNCR, IntakeStatus, RawMaterialIntake, Supplier

VIEW_PERM = "materials.view_rawmaterialintake"


class IntakeListView(PermissionRequiredMixin, ListView):
    permission_required = VIEW_PERM
    model = RawMaterialIntake
    template_name = "materials/intake_list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related("supplier", "grade")
        status = self.request.GET.get("status", "")
        return qs.filter(status=status) if status in IntakeStatus.values else qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(statuses=IntakeStatus.choices, status=self.request.GET.get("status", ""))
        return context


class IntakeCreateView(PermissionRequiredMixin, View):
    permission_required = services.PERM_CREATE

    def get(self, request):
        return render(request, "materials/intake_form.html", {"form": IntakeForm()})

    def post(self, request):
        form = IntakeForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            try:
                intake = services.create_intake(
                    request.user, supplier=data["supplier"], po_number=data["po_number"], grade=data["grade"],
                    heat_number=data["heat_number"], mtc_heat_number=data["mtc_heat_number"],
                    quantity=data["quantity"], unit=data["unit"], mtc_upload=data["mtc_file"],
                )
            except BusinessRuleViolation as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Intake {intake.lot_number} saved. Quality will now review the MTC.")
                return redirect("materials:detail", pk=intake.pk)
        return render(request, "materials/intake_form.html", {"form": form})


class IntakeDetailView(PermissionRequiredMixin, TemplateView):
    permission_required = VIEW_PERM
    template_name = "materials/intake_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        intake = get_object_or_404(
            RawMaterialIntake.objects.select_related("supplier", "grade", "received_by", "mtc_verified_by"),
            pk=self.kwargs["pk"],
        )
        user, S = self.request.user, IntakeStatus
        sncr = getattr(intake, "sncr", None)
        context.update(
            intake=intake, sncr=sncr, readings=intake.readings.select_related("grade_property"),
            properties=intake.grade.properties.all(), issues=intake.issues.select_related("work_order"),
            lab_result=getattr(intake, "lab_result", None), label=services.label_kind(intake),
            can_review=intake.status == S.RECEIVED and user.has_perm(services.PERM_REVIEW_MTC)
            and intake.received_by_id != user.pk,
            can_lab=intake.status == S.IN_LAB_TESTING and user.has_perm(services.PERM_LAB),
            can_reject=intake.status in (S.RECEIVED, S.IN_LAB_TESTING) and user.has_perm(services.PERM_REJECT),
            can_recall=intake.status == S.CLEARED and user.has_perm(services.PERM_QUARANTINE),
            can_resolve=bool(sncr and sncr.status == SNCR.Status.OPEN) and user.has_perm(services.PERM_RESOLVE),
            reject_form=RejectForm(), recall_form=RecallForm(), lab_form=LabResultForm(), resolve_form=ResolveForm(),
        )
        return context


class MTCFileView(PermissionRequiredMixin, View):
    permission_required = VIEW_PERM

    def get(self, request, pk):
        intake = get_object_or_404(RawMaterialIntake, pk=pk)
        kind = intake.mtc_file.name.rsplit(".", 1)[-1].lower()
        content_type = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png"}.get(kind)
        if content_type is None:
            raise Http404("Unsupported file.")
        response = FileResponse(intake.mtc_file.open("rb"), content_type=content_type)
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ActionView(PermissionRequiredMixin, View):
    """POST-only workflow buttons on the intake page; the rules are in services."""

    http_method_names = ["post"]
    success_message = ""

    def post(self, request, pk):
        intake = get_object_or_404(RawMaterialIntake, pk=pk)
        try:
            self.act(request, intake)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, self.success_message)
        return redirect("materials:detail", pk=pk)


class ReviewMTCView(ActionView):
    permission_required = services.PERM_REVIEW_MTC

    def act(self, request, intake):
        readings = {
            prop.pk: request.POST.get(f"prop_{prop.pk}", "") for prop in intake.grade.properties.all()
        }
        result = services.review_mtc(intake, request.user, readings)
        self.success_message = {
            IntakeStatus.CLEARED: "MTC verified. The lot is cleared for production.",
            IntakeStatus.IN_LAB_TESTING: "MTC verified. The lot is in lab testing; print the sample tag.",
        }.get(result.status, "MTC values are outside the specification. The lot is in Supplier Quarantine and an SNCR was raised.")


class LabResultView(ActionView):
    permission_required = services.PERM_LAB
    success_message = "Lab result recorded."

    def act(self, request, intake):
        form = LabResultForm(request.POST)
        if not form.is_valid():
            raise services.IntakeRejected("Choose pass or fail and enter the lab report reference.")
        d = form.cleaned_data
        services.record_lab_result(
            intake, request.user, passed=d["result"] == "pass", report_reference=d["report_reference"], remarks=d["remarks"]
        )


class RejectView(ActionView):
    permission_required = services.PERM_REJECT
    success_message = "Intake rejected. The stock is in Supplier Quarantine and an SNCR was raised."

    def act(self, request, intake):
        form = RejectForm(request.POST)
        if not form.is_valid():
            raise services.IntakeRejected("Select a reason and describe what was found.")
        services.reject_intake(intake, request.user, form.cleaned_data["reason_code"], form.cleaned_data["details"])


class RecallView(ActionView):
    permission_required = services.PERM_QUARANTINE
    success_message = "Lot quarantined. Work orders using it are blocked and an SNCR was raised."

    def act(self, request, intake):
        form = RecallForm(request.POST)
        if not form.is_valid():
            raise services.IntakeRejected("Explain why the lot is being quarantined.")
        services.recall_lot(intake, request.user, form.cleaned_data["details"])


class ResolveView(ActionView):
    permission_required = services.PERM_RESOLVE
    success_message = "SNCR closed."

    def act(self, request, intake):
        form = ResolveForm(request.POST)
        if not form.is_valid():
            raise services.InvalidTransition("Choose return to vendor or scrap and enter the reference.")
        services.resolve_sncr(intake.sncr, request.user, form.cleaned_data["resolution"], form.cleaned_data["reference"])


class LabelView(PermissionRequiredMixin, View):
    """Sample tag while in the lab; weather-proof bundle barcode only once cleared (PRD 3.2)."""

    permission_required = VIEW_PERM

    def get(self, request, pk):
        intake = get_object_or_404(RawMaterialIntake.objects.select_related("supplier", "grade"), pk=pk)
        kind = services.label_kind(intake)
        if kind is None:
            return render(request, "materials/label_locked.html", {"intake": intake}, status=403)
        return render(request, f"materials/label_{kind}.html", {"intake": intake})


class SNCRListView(PermissionRequiredMixin, ListView):
    permission_required = "materials.view_sncr"
    model = SNCR
    template_name = "materials/sncr_list.html"

    def get_queryset(self):
        return super().get_queryset().select_related("intake__supplier")


class SupplierListView(PermissionRequiredMixin, ListView):
    permission_required = "materials.view_supplier"
    model = Supplier
    template_name = "materials/supplier_list.html"


class IssueView(PermissionRequiredMixin, View):
    """Issue cleared stock to a work order, scanning the bundle barcode if wanted (PRD 3.2, 7.2)."""

    permission_required = services.PERM_ISSUE

    def get(self, request, pk):
        wo = get_object_or_404(WorkOrder, pk=pk)
        return render(request, "materials/issue.html", {"wo": wo, "form": IssueForm(work_order=wo)})

    def post(self, request, pk):
        wo = get_object_or_404(WorkOrder, pk=pk)
        form = IssueForm(request.POST, work_order=wo)
        if form.is_valid():
            d = form.cleaned_data
            try:
                intake = services.resolve_intake_from_barcode(d["lot_barcode"]) if d["lot_barcode"].strip() else d["intake"]
                if intake is None:
                    raise services.MaterialsError("Choose a cleared lot or scan its barcode.")
                services.issue_material(wo, intake, d["quantity"], request.user)
            except BusinessRuleViolation as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"{intake.lot_number} issued to {wo.number}.")
                return redirect("production:work_order_detail", pk=wo.pk)
        return render(request, "materials/issue.html", {"wo": wo, "form": form})
