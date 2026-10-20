from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import DetailView, ListView

from apps.core.exceptions import BusinessRuleViolation

from . import services
from .models import ChecklistTemplate, TemplateStatus


class TemplateListView(PermissionRequiredMixin, ListView):
    permission_required = "checklists.view_checklisttemplate"
    model = ChecklistTemplate
    template_name = "checklists/template_list.html"
    context_object_name = "templates_"

    def get_queryset(self):
        queryset = super().get_queryset().select_related("part", "created_by")
        status = self.request.GET.get("status")
        if status in TemplateStatus.values:
            queryset = queryset.filter(status=status)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["statuses"] = TemplateStatus.choices
        context["current_status"] = self.request.GET.get("status", "")
        return context


class TemplateDetailView(PermissionRequiredMixin, DetailView):
    permission_required = "checklists.view_checklisttemplate"
    model = ChecklistTemplate
    template_name = "checklists/template_detail.html"
    context_object_name = "tpl"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tpl, user = self.object, self.request.user
        context.update(
            fields=tpl.fields.select_related("pin"),
            pins=tpl.pins.all(),
            can_submit=tpl.status == TemplateStatus.DRAFT and user.has_perm(services.PERM_CHANGE),
            can_review=(
                tpl.status == TemplateStatus.PENDING_APPROVAL
                and user.has_perm(services.PERM_APPROVE)
                and tpl.created_by_id != user.pk
            ),
            can_revise=tpl.status == TemplateStatus.RELEASED and user.has_perm(services.PERM_ADD),
            can_edit=tpl.status == TemplateStatus.DRAFT and user.has_perm(services.PERM_CHANGE),
        )
        return context


class DrawingView(PermissionRequiredMixin, View):
    """Serves the drawing only to logged-in users with access (PRD 13.1 secure data)."""

    permission_required = "checklists.view_checklisttemplate"

    def get(self, request, pk):
        template = get_object_or_404(ChecklistTemplate, pk=pk)
        if not template.drawing_pdf:
            raise Http404("No drawing uploaded.")
        return FileResponse(template.drawing_pdf.open("rb"), content_type="application/pdf")


class WorkflowActionView(PermissionRequiredMixin, View):
    """Base for the POST-only workflow buttons. Rules are in services.py."""

    http_method_names = ["post"]
    success_message = ""

    def run(self, request, template):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request, pk):
        template = get_object_or_404(ChecklistTemplate, pk=pk)
        try:
            result = self.run(request, template)
        except BusinessRuleViolation as exc:
            messages.error(request, str(exc))
            return redirect("checklists:detail", pk=template.pk)
        messages.success(request, self.success_message)
        return redirect("checklists:detail", pk=result.pk)


class SubmitView(WorkflowActionView):
    permission_required = services.PERM_CHANGE
    success_message = "Template sent for approval."

    def run(self, request, template):
        return services.submit_for_approval(template, request.user)


class ApproveView(WorkflowActionView):
    permission_required = services.PERM_APPROVE
    success_message = "Template approved and released."

    def run(self, request, template):
        return services.approve_template(template, request.user)


class RejectView(WorkflowActionView):
    permission_required = services.PERM_APPROVE
    success_message = "Template sent back to the engineer."

    def run(self, request, template):
        return services.reject_template(template, request.user, request.POST.get("reason", ""))


class RevisionView(WorkflowActionView):
    permission_required = services.PERM_ADD
    success_message = "New draft revision created. Edit it in the admin site."

    def run(self, request, template):
        return services.create_revision(template, request.user)
