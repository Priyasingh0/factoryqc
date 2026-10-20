from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from apps.core.exceptions import BusinessRuleViolation
from . import presentation
from apps.production.models import JobTraveler

from . import services
from .exceptions import ShippingInterlock
from .forms import ManifestForm
from .models import GatePass, ShipmentLabel, ShipmentManifest, ShippingDocument

VIEW_PERM = "dispatch.view_shipmentmanifest"

# URL slug -> what it is; the three documents the interlock locks (PRD 10.2)
PRINTABLE = {
    "gate-pass": "dispatch gate pass",
    "packing-slip": "packing slip",
    "invoice": "commercial invoice",
}
_KIND = {"packing-slip": ShippingDocument.Kind.PACKING_SLIP, "invoice": ShippingDocument.Kind.INVOICE}


def _flash_block(request, exc):
    messages.error(request, str(exc))


class BoardView(PermissionRequiredMixin, TemplateView):
    """Every lot with its shipping-interlock state (PRD 10)."""

    permission_required = VIEW_PERM
    template_name = "dispatch/board.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = services.overview()
        counts = {key: 0 for key in presentation.LABELS}
        for row in rows:
            row["category"] = presentation.readiness(row["clearance"])
            row["category_label"] = presentation.LABELS[row["category"]]
            counts[row["category"]] += 1
        context["rows"] = rows
        context["counts"] = counts
        return context


class CheckView(PermissionRequiredMixin, TemplateView):
    """The green/red pre-dispatch check for one lot, with the exact unmet requirements."""

    permission_required = VIEW_PERM
    template_name = "dispatch/check.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        traveler = get_object_or_404(JobTraveler.objects.select_related("work_order__part"), pk=self.kwargs["pk"])
        result = services.clearance(traveler)
        user = self.request.user
        can_create = user.has_perm(services.PERM_MANIFEST) and result.cleared
        context.update(
            traveler=traveler, result=result,
            pdi=services.approved_final_pdi(traveler),
            manifests=traveler.manifests.select_related("gate_pass"),
            form=ManifestForm(traveler=traveler) if can_create else None,
        )
        return context


class ManifestCreateView(PermissionRequiredMixin, View):
    permission_required = services.PERM_MANIFEST

    def post(self, request, pk):
        traveler = get_object_or_404(JobTraveler, pk=pk)
        form = ManifestForm(request.POST, traveler=traveler)
        if not form.is_valid():
            messages.error(request, "Enter the vehicle number.")
            return redirect("dispatch:check", pk=pk)
        picked = list(form.cleaned_data["serials"]) or None
        try:
            manifest = services.create_manifest(
                traveler, request.user, customer_po=form.cleaned_data["customer_po"],
                vehicle_number=form.cleaned_data["vehicle_number"], serials=picked,
            )
        except BusinessRuleViolation as exc:
            _flash_block(request, exc)
            return redirect("dispatch:check", pk=pk)
        messages.success(request, f"Manifest {manifest.pk} created for {manifest.batch_size} parts.")
        return redirect("dispatch:manifest", pk=manifest.pk)


class ManifestDetailView(PermissionRequiredMixin, TemplateView):
    permission_required = VIEW_PERM
    template_name = "dispatch/manifest.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        manifest = get_object_or_404(
            ShipmentManifest.objects.select_related("traveler__work_order__part", "created_by"), pk=self.kwargs["pk"]
        )
        user = self.request.user
        result = services.clearance(manifest.traveler)
        docs = {d.kind: d for d in manifest.documents.all()}
        context.update(
            manifest=manifest, result=result, items=manifest.items.select_related("serial"),
            gate_pass=getattr(manifest, "gate_pass", None),
            packing_slip=docs.get(ShippingDocument.Kind.PACKING_SLIP),
            invoice=docs.get(ShippingDocument.Kind.INVOICE),
            labels=manifest.labels.all(),
            can_gate_pass=user.has_perm(services.PERM_GATE_PASS),
            can_documents=user.has_perm(services.PERM_DOCUMENTS),
        )
        return context


class IssueView(PermissionRequiredMixin, View):
    """POST: issue a gate pass, packing slip, invoice or crate label (PRD 10.2, 11)."""

    def get_permission_required(self):
        return [services.PERM_GATE_PASS if self.kwargs["what"] == "gate-pass" else services.PERM_DOCUMENTS]

    def post(self, request, pk, what):
        manifest = get_object_or_404(ShipmentManifest, pk=pk)
        try:
            if what == "gate-pass":
                services.issue_gate_pass(manifest, request.user)
                messages.success(request, "Gate pass issued. The shipment is marked dispatched.")
            elif what in _KIND:
                _, created = services.issue_document(manifest, _KIND[what], request.user)
                messages.success(request, f"{PRINTABLE[what].capitalize()} {'issued' if created else 'reprinted'}.")
            elif what == "label":
                label = services.issue_label(manifest, request.user)
                messages.success(request, f"QR label for crate {label.crate_number} created.")
                return redirect("dispatch:label", pk=label.pk)
            else:
                raise Http404
        except BusinessRuleViolation as exc:
            _flash_block(request, exc)
            return redirect("dispatch:manifest", pk=pk)
        return redirect("dispatch:print", pk=pk, what=what)


def _lockout(request, exc, title):
    return render(request, "dispatch/lockout.html", {"problems": exc.problems, "title": title}, status=403)


class PrintView(PermissionRequiredMixin, View):
    """Shows an issued gate pass, packing slip or invoice, re-checking the interlock first."""

    permission_required = VIEW_PERM

    def get(self, request, pk, what):
        if what not in PRINTABLE:
            raise Http404
        manifest = get_object_or_404(ShipmentManifest, pk=pk)
        try:
            manifest = services.authorize_print(manifest, request.user, PRINTABLE[what])
        except ShippingInterlock as exc:
            return _lockout(request, exc, PRINTABLE[what].capitalize())
        if what == "gate-pass":
            document = get_object_or_404(GatePass, manifest=manifest)
        else:
            document = get_object_or_404(ShippingDocument, manifest=manifest, kind=_KIND[what])
        pdi = services.approved_final_pdi(manifest.traveler)
        return render(request, f"dispatch/print_{what.replace('-', '_')}.html", {
            "manifest": manifest, "document": document, "items": manifest.items.select_related("serial"),
            "decision": pdi.decision if pdi else None,
            "concession": getattr(pdi.decision, "concession", None) if pdi else None,
        })


class LabelView(PermissionRequiredMixin, View):
    permission_required = VIEW_PERM

    def get(self, request, pk):
        label = get_object_or_404(ShipmentLabel.objects.select_related("manifest__traveler__work_order__part"), pk=pk)
        try:
            services.authorize_print(label.manifest, request.user, "QR label")
        except ShippingInterlock as exc:
            return _lockout(request, exc, "QR label")
        url = request.build_absolute_uri(reverse("dispatch:verify", args=[label.token]))
        return render(request, "dispatch/print_label.html", {"label": label, "manifest": label.manifest, "url": url})


@never_cache
def verify(request, token):
    """Public customer certificate behind the crate QR code (PRD 11.3). No login needed."""
    data = services.certificate(token)
    if data is None:
        return render(request, "dispatch/verify_invalid.html", status=404)
    return render(request, "dispatch/verify.html", data)
