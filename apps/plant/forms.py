from django import forms


class PMCheckForm(forms.Form):
    completed = forms.BooleanField(required=False, label="Daily PM checklist completed")
    safety_zero_point_ok = forms.BooleanField(
        required=False, label="Safety zero-point check passed"
    )
    remarks = forms.CharField(
        required=False, label="Remarks", widget=forms.Textarea(attrs={"rows": 2})
    )
