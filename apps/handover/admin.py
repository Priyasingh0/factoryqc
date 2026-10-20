from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyModelAdmin

from .models import HandoverAcknowledgement, HandoverSignoff, ShiftHandover


@admin.register(ShiftHandover)
class ShiftHandoverAdmin(ReadOnlyModelAdmin):
    list_display = ("machine", "shift_date", "shift", "outgoing_by", "machine_health", "tool_wear")
    list_filter = ("shift", "machine_health", "tool_wear")


@admin.register(HandoverSignoff)
class HandoverSignoffAdmin(ReadOnlyModelAdmin):
    list_display = ("handover", "signed_by", "signed_at")


@admin.register(HandoverAcknowledgement)
class HandoverAcknowledgementAdmin(ReadOnlyModelAdmin):
    list_display = ("handover", "acknowledged_by", "acknowledged_at")
