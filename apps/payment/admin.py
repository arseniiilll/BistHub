# -*- coding: utf-8 -*-
from django.contrib import admin, messages
import logging

from .models import Payment, PaymentAttempt, Refund, WebhookEvent
from .refund_service import RefundService
from .services import WebhookService

logger = logging.getLogger(__name__)


def _all_field_names_except(model, exclude=('id',)):
    return [f.name for f in model._meta.fields if f.name not in exclude]


class PaymentAttemptInline(admin.TabularInline):
    model = PaymentAttempt
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return _all_field_names_except(PaymentAttempt, exclude=('id', 'payment'))


class RefundInline(admin.TabularInline):
    model = Refund
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return _all_field_names_except(Refund, exclude=('id', 'payment'))


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'order', 'amount', 'currency', 'status', 'payment_method', 'created_at']
    list_filter = ['status', 'payment_method', 'currency', 'created_at']
    search_fields = ['user__email', 'user__username', 'stripe_payment_intent_id', 'stripe_session_id']
    inlines = [PaymentAttemptInline, RefundInline]
    ordering = ['-created_at']
    actions = ['refund_full_amount']

    def get_readonly_fields(self, request, obj=None):
        return _all_field_names_except(Payment)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Оформить полный возврат через Stripe')
    def refund_full_amount(self, request, queryset):
        succeeded, failed = 0, 0
        payment_ids = list(queryset.values_list('id', flat=True))
        fresh_payments = Payment.objects.filter(id__in=payment_ids).select_related('order', 'user')

        for payment in fresh_payments:
            try:
                RefundService.create_refund(
                    payment=payment,
                    amount=payment.refundable_amount,
                    reason='Возврат оформлен администратором через админку.',
                    created_by=request.user,
                )
                succeeded += 1
                logger.info('Refund created successfully for payment %s by admin %s', payment.id, request.user.id)
                self.message_user(
                    request,
                    f'Payment {payment.id}: возврат успешно оформлен',
                    level=messages.SUCCESS,
                )
            except ValueError as e:
                failed += 1
                logger.error('ValueError refunding payment %s: %s', payment.id, e)
                self.message_user(
                    request,
                    f'Payment {payment.id}: {e}',
                    level=messages.WARNING,
                )
            except Exception:
                failed += 1
                logger.exception('Unexpected error refunding payment %s', payment.id)
                self.message_user(
                    request,
                    f'Payment {payment.id}: неожиданная ошибка (см. логи сервера)',
                    level=messages.ERROR,
                )

        if succeeded:
            self.message_user(request, f'✓ Успешно возвращено платежей: {succeeded}', level=messages.SUCCESS)
        if failed:
            self.message_user(
                request,
                f'✗ Ошибок при возврате: {failed}',
                level=messages.WARNING if succeeded else messages.ERROR,
            )


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ['id', 'payment', 'amount', 'status', 'created_by', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['payment__id', 'stripe_refund_id']
    ordering = ['-created_at']

    def get_readonly_fields(self, request, obj=None):
        return _all_field_names_except(Refund)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ['id', 'provider', 'event_type', 'status', 'attempts', 'created_at']
    list_filter = ['provider', 'status', 'event_type']
    search_fields = ['event_id', 'event_type']
    ordering = ['-created_at']
    actions = ['retry_failed_webhook']

    def get_readonly_fields(self, request, obj=None):
        return _all_field_names_except(WebhookEvent)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Повторно обработать неудачные события')
    def retry_failed_webhook(self, request, queryset):
        retried = 0
        skipped = 0
        failed_to_retry = 0

        webhook_ids = list(queryset.filter(status='failed').values_list('id', flat=True))
        webhook_events = WebhookEvent.objects.filter(id__in=webhook_ids)

        for webhook_event in webhook_events:
            if not webhook_event.should_retry():
                skipped += 1
                self.message_user(
                    request,
                    f'Event {webhook_event.id}: превышено максимальное количество попыток '
                    f'({WebhookEvent.MAX_ATTEMPTS}), дальнейшие ретраи невозможны',
                    level=messages.WARNING,
                )
                continue

            try:
                WebhookService.reprocess_failed(webhook_event.event_id)
                retried += 1
                self.message_user(
                    request,
                    f'Event {webhook_event.id}: переобработка успешна',
                    level=messages.SUCCESS,
                )
            except ValueError as e:
                failed_to_retry += 1
                self.message_user(
                    request,
                    f'Event {webhook_event.id}: {e}',
                    level=messages.WARNING,
                )
            except Exception:
                failed_to_retry += 1
                logger.exception('Webhook %s reprocess failed', webhook_event.id)
                self.message_user(
                    request,
                    f'Event {webhook_event.id}: ошибка при переобработке (см. логи сервера)',
                    level=messages.ERROR,
                )

        if retried:
            self.message_user(request, f'✓ Переобработано событий: {retried}', level=messages.SUCCESS)
        if skipped:
            self.message_user(request, f'⊘ Пропущено событий (превышен лимит): {skipped}', level=messages.WARNING)
        if failed_to_retry:
            self.message_user(request, f'✗ Ошибок при переобработке: {failed_to_retry}', level=messages.ERROR)
