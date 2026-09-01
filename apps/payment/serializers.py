# -*- coding: utf-8 -*-
from rest_framework import serializers
from .models import Payment, Refund
from apps.orders.models import Order


class PaymentSerializer(serializers.ModelSerializer):
    """
    Для чтения — статус и все технические поля read-only.
    Они меняются только вебхуком/сервисным слоем, не клиентом.
    """

    class Meta:
        model = Payment
        fields = [
            'id', 'order', 'amount', 'currency', 'status',
            'payment_method', 'description', 'created_at',
            'updated_at', 'processed_at',
        ]
        read_only_fields = fields


class PaymentCreateSerializer(serializers.ModelSerializer):
    """Инициация или продолжение оплаты существующего заказа."""
    order_id = serializers.PrimaryKeyRelatedField(
        source='order', queryset=Order.objects.all(), write_only=True
    )

    class Meta:
        model = Payment
        fields = ['order_id', 'payment_method']

    def validate(self, attrs):
        request = self.context['request']
        order = attrs['order']

        if order.user != request.user:
            raise serializers.ValidationError('Это не ваш заказ.')

        valid_statuses = [
            getattr(getattr(order, 'StatusChoices', object), 'PENDING', 'pending')
        ]
        if hasattr(order, 'status') and order.status not in valid_statuses:
            raise serializers.ValidationError(
                'Заказ не может быть оплачен в текущем состоянии.'
            )

        if order.payments.filter(
            status__in=['succeeded', 'partially_refunded', 'refunded']
        ).exists():
            raise serializers.ValidationError('Заказ уже оплачен.')

        # Активный pending/processing платёж больше НЕ считаем ошибкой здесь.
        # PaymentService сам безопасно решит, можно ли переиспользовать
        # существующую Stripe Checkout Session, истекла ли она, либо нужно
        # создать новый Payment.
        return attrs

    def create(self, validated_data):
        raise NotImplementedError(
            "PaymentCreateSerializer.create() не используется напрямую — "
            "платёж инициируется через PaymentService.create_checkout_session."
        )


class RefundSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source='created_by.username',
        read_only=True,
        allow_null=True
    )

    class Meta:
        model = Refund
        fields = [
            'id', 'payment', 'amount', 'reason', 'status',
            'stripe_refund_id', 'created_by', 'created_by_username',
            'created_at', 'processed_at'
        ]
        read_only_fields = [
            'id', 'payment', 'amount', 'reason', 'status',
            'stripe_refund_id', 'created_by',
            'created_at', 'processed_at'
        ]