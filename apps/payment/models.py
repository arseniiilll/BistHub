# -*- coding: utf-8 -*-
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal

from apps.orders.models import Order


class Payment(models.Model):
    """Модель платежа"""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        SUCCEEDED = 'succeeded', 'Succeeded'
        PARTIALLY_REFUNDED = 'partially_refunded', 'Partially Refunded'
        REFUNDED = 'refunded', 'Refunded'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    class PaymentProvider(models.TextChoices):
        STRIPE = 'stripe', 'Stripe'

    # ISO 4217: молдавский/румынский лей = MDL/RON, а не 'LEU' (это разговорное
    # название, не код валюты).
    # TODO: проверить, что RON — это осознанный выбор (юрлицо/расчёты в
    # Румынии), а не опечатка вместо MDL. Смена значения задним числом
    # потребует миграции данных для уже существующих записей.
    # Это единственное место, где значение валюты по умолчанию задаётся
    # буквально — PaymentService.create_checkout_session() берёт его отсюда
    # (Payment.DEFAULT_CURRENCY), а не дублирует строковый литерал, чтобы при
    # смене валюты не пришлось синхронизировать два места вручную.
    DEFAULT_CURRENCY = 'RON'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='payments'
    )

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='payments')

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    payment_method = models.CharField(max_length=20, choices=PaymentProvider.choices, default=PaymentProvider.STRIPE)

    # Stripe-специфичные поля — единственное место в проекте, где они должны храниться.
    # unique=True (с null=True) не даёт двум разным Payment случайно схлопнуться
    # на один и тот же Stripe-объект при багах в коде обработки вебхуков —
    # Postgres допускает сколько угодно NULL, так что это не мешает полям
    # быть пустыми до момента, когда Stripe-сессия/intent реально создан(а).
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    stripe_session_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)

    # Метаданные (инициализируется как пустой dict, никогда не None)
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    # Временные метки
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'payments'
        verbose_name = 'Payment'
        verbose_name_plural = 'Payments'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['order', 'status']),
            models.Index(fields=['stripe_payment_intent_id']),
            models.Index(fields=['stripe_session_id']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Payment {self.id} - {self.user.username} - {self.currency}{self.amount} ({self.status})"

    @property
    def is_successful(self):
        return self.status == 'succeeded'

    @property
    def is_pending(self):
        return self.status in ['pending', 'processing']

    @property
    def can_be_refunded(self):
        """Платёж может быть возвращён, если он успешен или частично возвращён."""
        return self.status in ('succeeded', 'partially_refunded') and self.payment_method == self.PaymentProvider.STRIPE

    @property
    def refundable_amount(self):
        """
        Остаток, доступный к возврату: amount минус уже успешные И pending
        возвраты (pending резервируют сумму так же, как succeeded — см.
        RefundService.create_refund).

        ВАЖНО: само по себе свойство не берёт select_for_update(). Для
        получения гарантированно актуального значения под конкурентным
        доступом читайте его внутри транзакции с select_for_update() на
        Payment — как это делает RefundService.create_refund(). Используется,
        например, в PaymentAdmin.refund_full_amount(), чтобы "полный возврат"
        для уже частично возвращённого платежа возвращал именно остаток, а
        не исходную сумму платежа (иначе действие всегда падало бы с
        ValueError "сумма возврата превышает доступный остаток").
        """
        from django.db.models import Sum
        reserved = self.refunds.filter(
            status__in=['succeeded', 'pending']
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return self.amount - reserved

    def mark_as_succeeded(self):
        """
        Отметить платёж как успешный.

        ВАЖНО: этот метод НЕ меняет статус связанного Order. Если бизнес-логике
        нужна синхронизация статуса заказа при успешной оплате — она должна быть
        реализована отдельно (например, сигналом post_save на Payment в apps.orders,
        либо явным вызовом со стороны обработчика вебхука). Не полагайтесь на то,
        что вызов этого метода "сам" переведёт заказ в оплаченный статус.
        """
        from django.utils import timezone
        self.status = 'succeeded'
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at', 'updated_at', 'stripe_payment_intent_id'])

    def mark_as_failed(self, reason=None, stripe_payment_intent_id=None):
        """Отметить платёж как неудачный."""
        from django.utils import timezone
        self.status = 'failed'
        self.processed_at = timezone.now()
        update_fields = ['status', 'processed_at', 'updated_at']

        if reason:
            # Гарантируем, что metadata — это dict, не None
            if not isinstance(self.metadata, dict):
                self.metadata = {}
            self.metadata['failure_reason'] = reason
            update_fields.append('metadata')

        if stripe_payment_intent_id and not self.stripe_payment_intent_id:
            self.stripe_payment_intent_id = stripe_payment_intent_id
            update_fields.append('stripe_payment_intent_id')

        self.save(update_fields=update_fields)

    def mark_as_cancelled(self):
        """Отметить платёж как отменённый (при отмене заказа)."""
        from django.utils import timezone
        self.status = 'cancelled'
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at', 'updated_at'])

    def mark_as_partially_refunded(self):
        """Отметить платёж как частично возвращённый."""
        self.status = 'partially_refunded'
        # 'updated_at' обязательно в update_fields: при save(update_fields=[...])
        # Django обновляет auto_now-поля в БД, только если они явно перечислены.
        self.save(update_fields=['status', 'updated_at'])

    def mark_as_fully_refunded(self):
        self.status = 'refunded'
        self.save(update_fields=['status', 'updated_at'])


class PaymentAttempt(models.Model):
    """Попытки платежа"""

    class Status(models.TextChoices):
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'

    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name='attempts'
    )
    # unique=True: у одного платежа не может быть двух записей PaymentAttempt
    # с одним и тем же stripe_payment_intent_id. ВАЖНО: это осознанное
    # ограничение — Stripe может слать НЕСКОЛЬКО попыток оплаты (несколько
    # charges) в рамках ОДНОГО и того же PaymentIntent (например, клиент
    # ввёл невалидную карту, получил payment_intent.payment_failed, затем
    # ввёл другую карту в той же Checkout Session и получил успех). Такие
    # попытки делят один stripe_payment_intent_id, поэтому здесь хранится
    # не полная история каждой попытки, а ПОСЛЕДНЕЕ известное состояние
    # для данного intent — см. get_or_create + явное обновление статуса
    # в _handle_checkout_session_completed/_handle_payment_intent_payment_failed
    # в services.py (без этого явного обновления успешная повторная попытка
    # молча оставалась бы записанной как 'failed').
    stripe_payment_intent_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
    )
    status = models.CharField(max_length=50, choices=Status.choices)
    error_message = models.TextField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_attempts'
        verbose_name = 'Payment Attempt'
        verbose_name_plural = 'Payment Attempts'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['payment', 'status']),
            models.Index(fields=['stripe_payment_intent_id']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Attempt for Payment {self.payment.id} - {self.status}"


class Refund(models.Model):
    """Модель возврата средств"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name='refunds'
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    # unique=True: у двух разных Refund не может быть один и тот же
    # реальный возврат в Stripe
    stripe_refund_id = models.CharField(max_length=255, blank=True, null=True, unique=True)

    metadata = models.JSONField(default=dict, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_refunds'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'refunds'
        verbose_name = 'Refund'
        verbose_name_plural = 'Refunds'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['payment', 'status']),
            models.Index(fields=['stripe_refund_id']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Refund {self.id} - {self.payment.currency}{self.amount} for Payment {self.payment.id}"

    def is_partial(self):
        """
        Проверить, является ли этот возврат частичным.

        Учитываем уже проведённые ИЛИ зарезервированные (succeeded/pending)
        ДРУГИЕ возвраты по этому платежу, а также текущий возврат — та же
        логика резервирования суммы, что используется в
        Payment.refundable_amount и RefundService.create_refund(). Если
        учитывать здесь только 'succeeded', результат разойдётся с тем,
        сколько по платежу реально ещё можно вернуть (ещё не подтверждённые
        Stripe'ом pending-возвраты тоже блокируют часть суммы).
        Необходимо использовать внутри select_for_update() контекста для
        безопасности потоков.
        """
        from django.db.models import Sum
        other_reserved = self.payment.refunds.filter(
            status__in=['succeeded', 'pending']
        ).exclude(pk=self.pk).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return (other_reserved + self.amount) < self.payment.amount

    def mark_as_succeeded(self):
        """Отметить возврат как успешный."""
        from django.utils import timezone
        self.status = 'succeeded'
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at', 'stripe_refund_id'])

    def mark_as_failed(self, reason=None):
        from django.utils import timezone
        self.status = 'failed'
        self.processed_at = timezone.now()
        update_fields = ['status', 'processed_at']

        if reason:
            # Теперь это работает с существующим полем metadata
            if not isinstance(self.metadata, dict):
                self.metadata = {}
            self.metadata['failure_reason'] = reason
            update_fields.append('metadata')

        self.save(update_fields=update_fields)


class WebhookEvent(models.Model):
    """События webhook от платежных систем"""
    PROVIDER_CHOICES = [
        ('stripe', 'Stripe')
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
        ('ignored', 'Ignored'),
    ]

    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    # event_id должен быть уникален для идемпотентности вебхуков
    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    data = models.JSONField()
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, null=True)

    # Сколько раз пытались обработать это событие. Растёт при каждом
    # реальном вызове обработчика (в т.ч. при ручном/повторном reprocessing).
    # Максимум попыток обработки (dead-letter защита)
    attempts = models.PositiveIntegerField(default=0)
    MAX_ATTEMPTS = 5

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'webhook_events'
        verbose_name = 'Webhook Event'
        verbose_name_plural = 'Webhook Events'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['provider', 'event_type']),
            models.Index(fields=['status']),
            models.Index(fields=['created_at']),
            models.Index(fields=['event_id']),
        ]

    def __str__(self):
        return f"{self.provider} - {self.event_type} ({self.status})"

    def mark_as_processed(self):
        from django.utils import timezone
        self.status = 'processed'
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at'])

    def mark_as_failed(self, error_message):
        from django.utils import timezone
        self.status = 'failed'
        self.error_message = error_message
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'error_message', 'processed_at'])

    def should_retry(self):
        """Проверить, нужно ли ретраить это событие."""
        return self.status == 'failed' and self.attempts < self.MAX_ATTEMPTS

    def increment_attempts(self):
        """
        Атомарно увеличить счётчик попыток обработки.

        Используется из WebhookService.process_event() и reprocess_failed().
        Обновление через F() (а не self.attempts += 1; self.save()) исключает
        race condition при потенциально параллельном инкременте одного и того
        же события (например, гонка между обычной доставкой вебхука и ручным
        retry из админки).
        """
        type(self).objects.filter(pk=self.pk).update(attempts=models.F('attempts') + 1)
        self.refresh_from_db(fields=['attempts'])