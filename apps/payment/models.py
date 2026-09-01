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
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentProvider.choices,
        default=PaymentProvider.STRIPE
    )
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    stripe_session_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
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
        return self.status == self.Status.SUCCEEDED

    @property
    def is_pending(self):
        return self.status in (self.Status.PENDING, self.Status.PROCESSING)

    @property
    def can_be_refunded(self):
        return self.status in (
            self.Status.SUCCEEDED,
            self.Status.PARTIALLY_REFUNDED,
        ) and self.payment_method == self.PaymentProvider.STRIPE

    @property
    def refundable_amount(self):
        from django.db.models import Sum
        reserved = self.refunds.filter(
            status__in=['succeeded', 'pending']
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return self.amount - reserved

    def mark_as_succeeded(self):
        """Отметить платёж успешным и перевести ожидающий заказ в обработку."""
        from django.utils import timezone
        from django.db import transaction

        with transaction.atomic():
            self.status = self.Status.SUCCEEDED
            self.processed_at = timezone.now()
            self.save(update_fields=[
                'status', 'processed_at', 'updated_at',
                'stripe_payment_intent_id'
            ])

            # Оплаченный заказ больше не должен оставаться pending.
            # update() делает операцию идемпотентной: уже processing/shipped/
            # delivered заказ мы не откатываем назад.
            Order.objects.filter(
                pk=self.order_id,
                status=Order.StatusChoices.PENDING,
            ).update(status=Order.StatusChoices.PROCESSING)

    def mark_as_failed(self, reason=None, stripe_payment_intent_id=None):
        from django.utils import timezone
        self.status = self.Status.FAILED
        self.processed_at = timezone.now()
        update_fields = ['status', 'processed_at', 'updated_at']
        if reason:
            if not isinstance(self.metadata, dict):
                self.metadata = {}
            self.metadata['failure_reason'] = reason
            update_fields.append('metadata')
        if stripe_payment_intent_id and not self.stripe_payment_intent_id:
            self.stripe_payment_intent_id = stripe_payment_intent_id
            update_fields.append('stripe_payment_intent_id')
        self.save(update_fields=update_fields)

    def mark_as_cancelled(self):
        from django.utils import timezone
        self.status = self.Status.CANCELLED
        self.processed_at = timezone.now()
        self.save(update_fields=['status', 'processed_at', 'updated_at'])

    def mark_as_partially_refunded(self):
        self.status = self.Status.PARTIALLY_REFUNDED
        self.save(update_fields=['status', 'updated_at'])

    def mark_as_fully_refunded(self):
        self.status = self.Status.REFUNDED
        self.save(update_fields=['status', 'updated_at'])


class PaymentAttempt(models.Model):
    class Status(models.TextChoices):
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='attempts')
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
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
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='refunds')
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
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
        from django.db.models import Sum
        other_reserved = self.payment.refunds.filter(
            status__in=['succeeded', 'pending']
        ).exclude(pk=self.pk).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return (other_reserved + self.amount) < self.payment.amount

    def mark_as_succeeded(self):
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
            if not isinstance(self.metadata, dict):
                self.metadata = {}
            self.metadata['failure_reason'] = reason
            update_fields.append('metadata')
        self.save(update_fields=update_fields)


class WebhookEvent(models.Model):
    MAX_ATTEMPTS = 5
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
        ('ignored', 'Ignored'),
    ]

    provider = models.CharField(max_length=20, default='stripe')
    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'webhook_events'
        ordering = ['-created_at']

    def increment_attempts(self):
        self.attempts += 1
        self.save(update_fields=['attempts'])

    def should_retry(self):
        return self.attempts < self.MAX_ATTEMPTS

    def mark_as_processed(self):
        from django.utils import timezone
        self.status = 'processed'
        self.processed_at = timezone.now()
        self.error_message = None
        self.save(update_fields=['status', 'processed_at', 'error_message'])

    def mark_as_failed(self, reason=None):
        from django.utils import timezone
        self.status = 'failed'
        self.processed_at = timezone.now()
        self.error_message = reason or ''
        self.save(update_fields=['status', 'processed_at', 'error_message'])
