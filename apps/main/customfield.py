import decimal

from rest_framework import serializers

class PriceField(serializers.IntegerField):
    def to_representation(self, value):
        return f"RON {str(decimal.Decimal(value))}"

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = data.replace("RON", "").strip()
        return super().to_internal_value(data)