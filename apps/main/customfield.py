from decimal import Decimal

from rest_framework import serializers


class PriceField(serializers.DecimalField):
    """Decimal price field that keeps the existing `RON 15.00` API format."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_digits', 10)
        kwargs.setdefault('decimal_places', 2)
        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        # Let DRF normalise/quantize the Decimal first, then add the currency
        # prefix expected by the existing storefront API contract.
        formatted = super().to_representation(value)
        return f'RON {formatted}'

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = data.strip()
            if data.upper().startswith('RON'):
                data = data[3:].strip()
        value = super().to_internal_value(data)
        return Decimal(value)
