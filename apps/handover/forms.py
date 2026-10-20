from django import forms

from .models import MachineHealth, ToolWear

NOTES = {"rows": 3}


class HandoverForm(forms.Form):
    scrap_count = forms.IntegerField(min_value=0, label="Scrap count this shift")
    tool_wear = forms.ChoiceField(choices=ToolWear.choices, widget=forms.RadioSelect, label="Tool wear status")
    tool_wear_notes = forms.CharField(required=False, widget=forms.Textarea(attrs=NOTES), label="Tool wear notes")
    machine_health = forms.ChoiceField(choices=MachineHealth.choices, widget=forms.RadioSelect, label="Machine health")
    machine_health_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs=NOTES), label="Machine health notes"
    )
    job_status_notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs=NOTES), label="Status of the running job for the next shift"
    )


class RemarksForm(forms.Form):
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs=NOTES), label="Remarks (optional)")
