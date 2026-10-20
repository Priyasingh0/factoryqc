from django import forms

from .models import SNCR, MaterialGrade, RawMaterialIntake, Supplier


class IntakeForm(forms.Form):
    """PRD 3.1: supplier, PO, grade, heat number, quantity and the MTC scan (3.2)."""

    supplier = forms.ModelChoiceField(queryset=Supplier.objects.filter(is_active=True))
    po_number = forms.CharField(label="Purchase order number", max_length=50)
    grade = forms.ModelChoiceField(label="Material grade", queryset=MaterialGrade.objects.filter(is_active=True))
    heat_number = forms.CharField(label="Heat number", max_length=50)
    mtc_heat_number = forms.CharField(label="Heat number printed on the MTC", max_length=50)
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=0)
    unit = forms.ChoiceField(choices=RawMaterialIntake.Unit.choices)
    mtc_file = forms.FileField(
        label="Mill test certificate (scan or upload)",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,image/*", "capture": "environment"}),
    )


class RejectForm(forms.Form):
    reason_code = forms.ChoiceField(
        label="Why is the intake rejected?",
        choices=[("", "Select")] + [c for c in SNCR.Reason.choices if c[0] != SNCR.Reason.RECALL],
    )
    details = forms.CharField(label="What was found", widget=forms.Textarea(attrs={"rows": 3}))


class RecallForm(forms.Form):
    details = forms.CharField(label="Why is this cleared lot being quarantined?", widget=forms.Textarea(attrs={"rows": 3}))


class LabResultForm(forms.Form):
    result = forms.ChoiceField(choices=[("pass", "Pass"), ("fail", "Fail")], widget=forms.RadioSelect)
    report_reference = forms.CharField(label="Lab report reference", max_length=80)
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class ResolveForm(forms.Form):
    resolution = forms.ChoiceField(choices=[("", "Select")] + list(SNCR.Resolution.choices))
    reference = forms.CharField(label="Debit note reference / scrap cost code", max_length=60)


class IssueForm(forms.Form):
    """Pick a cleared lot, or scan its bundle barcode (PRD 3.2), and say how much is issued."""

    intake = forms.ModelChoiceField(label="Cleared lot", queryset=RawMaterialIntake.objects.none(), required=False)
    lot_barcode = forms.CharField(
        label="Or scan the bundle barcode", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=0)

    def __init__(self, *args, work_order=None, **kwargs):
        super().__init__(*args, **kwargs)
        if work_order is not None:
            self.fields["intake"].queryset = RawMaterialIntake.objects.filter(
                status="CLEARED", heat_number__iexact=work_order.heat_number.strip()
            )
