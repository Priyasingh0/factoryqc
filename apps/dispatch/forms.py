from django import forms

from apps.production.models import PartSerial


class ManifestForm(forms.Form):
    """PRD 11: customer PO, transport vehicle and the serial numbers on the truck."""

    customer_po = forms.CharField(label="Customer PO", max_length=50, required=False)
    vehicle_number = forms.CharField(label="Transport vehicle number", max_length=30)
    serials = forms.ModelMultipleChoiceField(
        label="Parts on this shipment", queryset=PartSerial.objects.none(),
        widget=forms.CheckboxSelectMultiple, required=False,
        help_text="Leave all unticked to ship every part that has not shipped yet.",
    )

    def __init__(self, *args, traveler=None, **kwargs):
        super().__init__(*args, **kwargs)
        if traveler is not None:
            self.fields["serials"].queryset = traveler.serials.filter(manifest_item__isnull=True)
            self.fields["customer_po"].initial = traveler.work_order.customer_po
