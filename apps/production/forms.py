from django import forms


class StartOperationForm(forms.Form):
    """PRD 5.1: both codes must be scanned; there is no manual job or station search."""

    machine_qr = forms.CharField(label="Station QR code", widget=forms.Textarea(attrs={"rows": 2}))
    traveler_code = forms.CharField(label="Traveler barcode", widget=forms.Textarea(attrs={"rows": 2}))


class RecordPartsForm(forms.Form):
    count = forms.IntegerField(min_value=1, label="Parts completed")


class TraceForm(forms.Form):
    KIND_CHOICES = [("heat", "Heat number (forward recall)"), ("serial", "Part serial (birth certificate)")]
    kind = forms.ChoiceField(choices=KIND_CHOICES, label="Search by")
    q = forms.CharField(label="Value", max_length=100)
