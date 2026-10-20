from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyModelAdmin

from .models import Concession, MRBDisposition, MRBSignoff, QuarantineRecord, SupervisorDecision


@admin.register(SupervisorDecision)
class SupervisorDecisionAdmin(ReadOnlyModelAdmin):
    list_display = ("inspection", "decision", "decided_by", "decided_at")
    list_filter = ("decision",)


@admin.register(Concession)
class ConcessionAdmin(ReadOnlyModelAdmin):
    list_display = ("decision", "reason_code", "issued_by", "issued_at")


@admin.register(QuarantineRecord)
class QuarantineRecordAdmin(ReadOnlyModelAdmin):
    list_display = ("traveler", "status", "location", "created_at")
    list_filter = ("status",)


@admin.register(MRBDisposition)
class MRBDispositionAdmin(ReadOnlyModelAdmin):
    list_display = ("quarantine", "disposition", "proposed_by", "decided_at")


@admin.register(MRBSignoff)
class MRBSignoffAdmin(ReadOnlyModelAdmin):
    list_display = ("disposition", "role", "user", "signed_at")
