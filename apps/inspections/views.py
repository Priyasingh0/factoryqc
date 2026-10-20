from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView, ListView

from apps.checklists.models import FieldType
from apps.core.exceptions import BusinessRuleViolation

from . import services
from .exceptions import InvalidMeasurement
from .forms import PhotoForm, StartInspectionForm, SubmitForm
from .models import DefectPhoto, Inspection, InspectionStatus, Measurement

VIEW_PERM = "inspections.view_inspection"


class InspectionListView(PermissionRequiredMixin, ListView):
    permission_required = VIEW_PERM
    model = Inspection
    template_name = "inspections/inspection_list.html"
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related("traveler__work_order__part", "machine", "inspector")
        status = self.request.GET.get("status", "")
        if status in InspectionStatus.values:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["statuses"] = InspectionStatus.choices
        context["current_status"] = self.request.GET.get("status", "")
        context["my_open"] = Inspection.objects.filter(
            inspector=self.request.user, status=InspectionStatus.IN_PROGRESS
        ).first()
        return context


class StartInspectionView(PermissionRequiredMixin, View):
    permission_required = services.PERM_CONDUCT
    template_name = "inspections/start.html"

    def get(self, request):
        return render(request, self.template_name, {"form": StartInspectionForm()})

    def post(self, request):
        form = StartInspectionForm(request.POST)
        if form.is_valid():
            try:
                inspection = services.start_inspection(
                    request.user, form.cleaned_data["machine_qr"], form.cleaned_data["traveler_code"],
                    form.cleaned_data["checklist_type"],
                )
            except BusinessRuleViolation as exc:
                messages.error(request, str(exc))
            else:
                return redirect("inspections:detail", pk=inspection.pk)
        return render(request, self.template_name, {"form": form})


class InspectionDetailView(PermissionRequiredMixin, DetailView):
    permission_required = VIEW_PERM
    model = Inspection
    template_name = "inspections/inspection_detail.html"
    context_object_name = "inspection"

    def get_queryset(self):
        return super().get_queryset().select_related(
            "template", "traveler__work_order__part", "machine", "inspector", "traveler_operation__routing_operation"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        inspection, user = self.object, self.request.user
        mine = inspection.inspector_id == user.pk and user.has_perm(services.PERM_CONDUCT)
        done = {m.field_id: m for m in inspection.measurements.select_related("gauge").prefetch_related("photos")}
        rows = []
        for field in inspection.template.fields.all():
            m = done.get(field.pk)
            photos = list(m.photos.all()) if m else []
            failed = m is not None and not m.is_within_tolerance
            rows.append({
                "field": field,
                "measurement": m,
                "photos": photos,
                "visual": field.field_type == FieldType.VISUAL,
                "can_enter": mine and inspection.is_open and m is None,
                "can_photo": (
                    mine and failed
                    and inspection.status in (InspectionStatus.IN_PROGRESS, InspectionStatus.FROZEN)
                    and len(photos) < services.MAX_DEFECT_PHOTOS
                ),
                "photos_needed": max(0, services.MIN_DEFECT_PHOTOS - len(photos)) if failed else 0,
            })
        problems = services.submission_problems(inspection) if inspection.is_open else []
        context.update(
            rows=rows,
            problems=problems,
            can_submit=mine and inspection.is_open and not problems,
            show_submit=mine and inspection.is_open,
            photo_form=PhotoForm(),
            submit_form=SubmitForm(),
            min_photos=services.MIN_DEFECT_PHOTOS,
            measured=len(done),
            total=len(rows),
        )
        return context


class InspectionActionView(PermissionRequiredMixin, View):
    """Base for the POST-only tablet buttons; every rule lives in services.py."""

    permission_required = services.PERM_CONDUCT
    http_method_names = ["post"]

    def perform(self, request, **kwargs):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, **kwargs):
        inspection_pk = self.perform(request, **kwargs)
        return redirect("inspections:detail", pk=inspection_pk)


class MeasureView(InspectionActionView):
    def perform(self, request, pk, field_pk):
        inspection = get_object_or_404(Inspection.objects.select_related("template"), pk=pk)
        field = get_object_or_404(inspection.template.fields, pk=field_pk)
        try:
            if field.field_type == FieldType.VISUAL:
                answer = request.POST.get("result")
                if answer not in ("PASS", "FAIL"):
                    raise InvalidMeasurement("Choose Pass or Fail.")
                value = answer == "PASS"
            else:
                value = request.POST.get("value", "")
            measurement = services.record_measurement(
                inspection, field, value=value, gauge_id=request.POST.get("gauge_id", ""), user=request.user
            )
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
            return inspection.pk
        inspection.refresh_from_db()
        if measurement.is_within_tolerance:
            messages.success(request, f"GREEN: {field.label} = {measurement.display_value} is within tolerance.")
        elif inspection.status == InspectionStatus.FROZEN:
            messages.error(
                request,
                f"RED, CTQ FAILURE: {field.label} = {measurement.display_value}. The checklist is frozen and "
                f"the Quality Head has been alerted. Upload at least {services.MIN_DEFECT_PHOTOS} defect photos.",
            )
        else:
            messages.error(
                request,
                f"RED: {field.label} = {measurement.display_value} is OUT of tolerance. "
                f"Upload at least {services.MIN_DEFECT_PHOTOS} defect photos before submitting.",
            )
        return inspection.pk


class PhotoUploadView(InspectionActionView):
    def perform(self, request, pk):
        measurement = get_object_or_404(Measurement, pk=pk)
        try:
            services.add_defect_photo(measurement, request.FILES.get("image"), request.user)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Photo saved.")
        return measurement.inspection_id


class SubmitView(InspectionActionView):
    def perform(self, request, pk):
        inspection = get_object_or_404(Inspection, pk=pk)
        form = SubmitForm(request.POST)
        try:
            services.submit_inspection(
                inspection, request.user, form.cleaned_data["remarks"] if form.is_valid() else ""
            )
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Inspection submitted. It is now awaiting supervisor approval.")
        return inspection.pk


class PhotoView(PermissionRequiredMixin, View):
    """Serves a defect photo only to logged-in users with access (PRD 13.1)."""

    permission_required = "inspections.view_defectphoto"

    def get(self, request, pk):
        photo = get_object_or_404(DefectPhoto, pk=pk)
        if not photo.image:
            raise Http404("No file.")
        kind = photo.image.name.rsplit(".", 1)[-1].lower()
        content_type = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(kind)
        if content_type is None:
            raise Http404("Unsupported file.")
        response = FileResponse(photo.image.open("rb"), content_type=content_type)
        response["X-Content-Type-Options"] = "nosniff"
        return response
