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
    """
    Инициация платежа по существующему заказу.

    ВАЖНО: validate() здесь делает только "мягкую", НЕзалоченную проверку —
    быстрый и понятный ответ пользователю до похода в Stripe. Настоящая
    защита от race conditions (select_for_update() внутри транзакции)
    реализована в PaymentService.create_checkout_session(), который
    перепроверяет те же условия ещё раз под локом перед созданием платежа.
    Не полагайтесь на эту проверку здесь как на единственную защиту —
    без повторной проверки в сервисе два параллельных запроса всё ещё
    могли бы проскочить оба этих условия одновременно.
    """
    order_id = serializers.PrimaryKeyRelatedField(
        source='order', queryset=Order.objects.all(), write_only=True
    )

    class Meta:
        model = Payment
        fields = ['order_id', 'payment_method']

    def validate(self, attrs):
        request = self.context['request']
        order = attrs['order']

        # Проверка: платёж принадлежит текущему пользователю
        if order.user != request.user:
            raise serializers.ValidationError('Это не ваш заказ.')

        # Проверка: заказ в валидном состоянии для оплаты.
        # Оплата возможна только для заказа в статусе PENDING (только что
        # оформлен, ещё не в обработке/не отменён/не доставлен).
        # ВАЖНО: раньше здесь бралось order.StatusChoices.choices ЦЕЛИКОМ —
        # то есть буквально ВСЕ статусы enum'а (pending/processing/shipped/
        # delivered/canceled), из-за чего проверка была фактическим no-op:
        # order.status оказывался "валиден для оплаты" при любом значении.
        # Заказ в статусе canceled/delivered можно было бы "оплатить" через
        # этот эндпоинт. Теперь явно разрешён только PENDING.
        if hasattr(order, 'StatusChoices') and hasattr(order.StatusChoices, 'PENDING'):
            valid_statuses = [order.StatusChoices.PENDING]
        else:
            # Fallback на случай, если Order.StatusChoices не определён так,
            # как ожидается — явное безопасное значение по умолчанию, а не
            # "всё, что найдётся" в enum'е.
            valid_statuses = ['pending']

        if hasattr(order, 'status') and order.status not in valid_statuses:
            raise serializers.ValidationError(
                'Заказ не может быть оплачен в текущем состоянии.'
            )

        # Проверка: заказ уже оплачен.
        # ВАЖНО: сюда намеренно включены 'partially_refunded' и 'refunded',
        # а не только 'succeeded'. Payment.mark_as_partially_refunded()/
        # mark_as_fully_refunded() меняют status ПРОЧЬ от 'succeeded' — если
        # проверять только 'succeeded', заказ, по которому уже был возврат
        # (полный или частичный), перестаёт считаться оплаченным для этой
        # проверки, и клиент мог бы инициировать повторную оплату того же
        # заказа.
        if order.payments.filter(
            status__in=['succeeded', 'partially_refunded', 'refunded']
        ).exists():
            raise serializers.ValidationError('Заказ уже оплачен.')

        # Проверка: нет активных платежей (pending/processing).
        # Это обычный (незалоченный) SELECT — окончательную защиту от
        # гонки даёт повторная проверка под select_for_update() в
        # PaymentService.create_checkout_session(), а не эта строка.
        if order.payments.filter(status__in=['pending', 'processing']).exists():
            raise serializers.ValidationError(
                'Заказ уже находится в процессе оплаты. Пожалуйста, дождитесь завершения.'
            )

        return attrs

    def create(self, validated_data):
        # Payment создаётся только через PaymentService.create_checkout_session
        # (там же берётся блокировка на заказ и создаётся Stripe-сессия).
        # Этот метод намеренно не реализован.
        raise NotImplementedError(
            "PaymentCreateSerializer.create() не используется напрямую — "
            "платёж инициируется через PaymentService.create_checkout_session."
        )


class RefundSerializer(serializers.ModelSerializer):
    """
    Сериализатор для возвратов средств.

    Только для чтения. Возврат создаётся ИСКЛЮЧИТЕЛЬНО через
    RefundService.create_refund(), который сам проверяет can_be_refunded()
    и остаток по платежу (с учётом уже зарезервированных pending-возвратов)
    внутри select_for_update(). 'payment', 'amount' и 'reason' намеренно
    включены в read_only_fields: если этот сериализатор когда-либо подключат
    к write-эндпоинту (POST/PUT), клиент НЕ должен иметь возможность указать
    платёж и сумму возврата напрямую — это позволило бы оформить возврат на
    произвольную сумму в обход всех финансовых проверок сервиса.
    """
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