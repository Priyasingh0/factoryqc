from django import forms

from apps.checklists.models import ChecklistType


class StartInspectionForm(forms.Form):
    """PRD 5.1: both codes are scanned; there is no job or station search."""

    checklist_type = forms.ChoiceField(
        label="Inspection",
        choices=[(ChecklistType.FIRST_PIECE, "First-piece inspection"), (ChecklistType.PDI, "Final PDI")],
    )
    machine_qr = forms.CharField(label="Station QR code", widget=forms.Textarea(attrs={"rows": 2}))
    traveler_code = forms.CharField(label="Traveler barcode", widget=forms.Textarea(attrs={"rows": 2}))


class PhotoForm(forms.Form):
    image = forms.FileField(label="Defect photo")


class SubmitForm(forms.Form):
    remarks = forms.CharField(label="Inspector remarks", required=False, widget=forms.Textarea(attrs={"rows": 2}))
