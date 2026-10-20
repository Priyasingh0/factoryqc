from django import forms

from .models import Disposition, ReasonCode


class ApproveForm(forms.Form):
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ConcessionForm(forms.Form):
    reason_code = forms.ChoiceField(choices=[("", "Select a reason code")] + list(ReasonCode.choices))
    justification = forms.CharField(label="Engineering justification", widget=forms.Textarea(attrs={"rows": 4}))


class QuarantineForm(forms.Form):
    reason = forms.CharField(label="Reason for rejection", widget=forms.Textarea(attrs={"rows": 3}))


class DispositionForm(forms.Form):
    disposition = forms.ChoiceField(choices=[("", "Select")] + list(Disposition.choices))
    notes = forms.CharField(label="Corrective action / reason", widget=forms.Textarea(attrs={"rows": 3}))
    rework_from_op = forms.IntegerField(label="Rework restarts at op number", required=False, min_value=1)
    scrap_cost_code = forms.CharField(required=False, max_length=40)
    scrap_bin_verified = forms.BooleanField(label="Verified at the scrap bin station", required=False)
    debit_note_ref = forms.CharField(label="Debit note reference", required=False, max_length=60)


class CustomerApprovalForm(forms.Form):
    document = forms.FileField(label="Customer's written approval (PDF, JPEG or PNG)")
