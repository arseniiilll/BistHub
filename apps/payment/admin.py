# -*- coding: utf-8 -*-
from django.contrib import admin, messages
import logging

from .models import Payment, PaymentAttempt, Refund, WebhookEvent
from .services import RefundService, WebhookService

logger = logging.getLogger(__name__)


def _all_field_names_except(model, exclude=('id',)):
    """
    Вернуть имена ВСЕХ конкретных полей модели, кроме перечисленных в exclude.

    ЗАЧЕМ: readonly_fields для read-only админок ниже раньше задавались
    статическим списком, который нужно было вручную синхронизировать с
    моделью. Это уже дважды приводило к одной и той же дыре: при добавлении
    нового поля в модель (сначала 'metadata' у PaymentAttempt, потом
    'reason'/'metadata' у Refund) о нём просто забывали в admin.py — и поле
    оставалось редактируемым в обход сервисного слоя, хотя вся модель
    задумывалась как строго read-only через админку.

    Вычисляя список полей динамически из model._meta, мы делаем так, чтобы
    ЛЮБОЕ новое поле, добавленное в модель в будущем, автоматически становилось
    read-only без необходимости помнить об обновлении admin.py — тем самым
    класс этой ошибки закрывается структурно, а не точечным патчем.
    """
    return [f.name for f in model._meta.fields if f.name not in exclude]


class PaymentAttemptInline(admin.TabularInline):
    model = PaymentAttempt
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        # Все поля модели, кроме id и 'payment' (FK на родителя — и так
        # скрыт самим инлайном, но исключаем явно для чистоты списка).
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
    """
    Админ-панель для платежей.

    КРИТИЧЕСКИ ВАЖНО:
    - Все финансовые операции выполняются ТОЛЬКО через PaymentService/RefundService
    - Ручное редактирование создаёт расхождение между БД и Stripe
    - select_for_update() применяется внутри сервисов, не в админ-экшене
    """
    list_display = ['id', 'user', 'order', 'amount', 'currency', 'status', 'payment_method', 'created_at']
    list_filter = ['status', 'payment_method', 'currency', 'created_at']
    search_fields = ['user__email', 'user__username', 'stripe_payment_intent_id', 'stripe_session_id']

    def get_readonly_fields(self, request, obj=None):
        return _all_field_names_except(Payment)

    inlines = [PaymentAttemptInline, RefundInline]
    ordering = ['-created_at']
    actions = ['refund_full_amount']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Оформить полный возврат через Stripe')
    def refund_full_amount(self, request, queryset):
        """
        Администраторское действие: возврат ОСТАВШЕЙСЯ доступной суммы для
        выбранных платежей (для ещё не тронутого платежа это совпадает с
        payment.amount; для уже частично возвращённого — именно остаток).

        АРХИТЕКТУРА:
        - Каждый платёж обрабатывается в отдельной итерации
        - RefundService.create_refund() сам берёт select_for_update() на платёж
        - Повторное прочитывание платежа перед проверкой предотвращает TOCTOU-ошибки
        - Ошибки логируются и отображаются пользователю

        ВАЖНО: используем payment.refundable_amount, а НЕ payment.amount.
        Для платежа со статусом 'partially_refunded' payment.amount — это
        исходная (полная) сумма платежа; если передать её в create_refund(),
        сервис всегда будет отклонять запрос как превышающий доступный
        остаток. refundable_amount вычисляет именно то, что реально можно
        вернуть с учётом уже проведённых/зарезервированных возвратов.
        """
        succeeded, failed = 0, 0
        payment_ids = list(queryset.values_list('id', flat=True))

        # Перезагружаем платежи свежими данными
        fresh_payments = Payment.objects.filter(id__in=payment_ids).select_related('order', 'user')

        for payment in fresh_payments:
            try:
                # RefundService сам проверяет can_be_refunded() и выбросит ValueError если нельзя
                RefundService.create_refund(
                    payment=payment,
                    amount=payment.refundable_amount,
                    reason='Возврат оформлен администратором через админку.',
                    created_by=request.user,
                )
                succeeded += 1
                logger.info(f"Refund created successfully for payment {payment.id} by admin {request.user.id}")
                self.message_user(
                    request,
                    f"Payment {payment.id}: возврат успешно оформлен",
                    level=messages.SUCCESS
                )
            except ValueError as e:
                failed += 1
                error_msg = str(e)
                logger.error(f"ValueError refunding payment {payment.id}: {error_msg}")
                self.message_user(
                    request,
                    f"Payment {payment.id}: {error_msg}",
                    level=messages.WARNING,
                )
            except Exception:
                failed += 1
                logger.exception(f"Unexpected error refunding payment {payment.id}")
                self.message_user(
                    request,
                    f"Payment {payment.id}: неожиданная ошибка (см. логи сервера)",
                    level=messages.ERROR,
                )

        if succeeded:
            self.message_user(
                request,
                f"✓ Успешно возвращено платежей: {succeeded}",
                level=messages.SUCCESS
            )
        if failed:
            self.message_user(
                request,
                f"✗ Ошибок при возврате: {failed}",
                level=messages.WARNING if succeeded else messages.ERROR,
            )


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """
    Админ-панель для возвратов.

    ВАЖНО: Возврат создаётся и проводится только через RefundService.
    Ручное редактирование создаёт "возврат", которого по факту нет в Stripe.
    """
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
    """
    Админ-панель для вебхук-событий от Stripe.

    КРИТИЧНО: status и attempts должны быть ТОЛЬКО read-only!

    Если сбросить status на 'pending', Stripe при повторной доставке
    может задвоить обработку события (особенно для charge.refunded).
    """
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
        """
        Администраторское действие: повторная обработка неудачных вебхуков.

        АРХИТЕКТУРА:
        - Используется WebhookService.reprocess_failed(event_id)
        - reprocess_failed() самостоятельно инкрементирует attempts
        - Это предотвращает двойное инкрементирование
        """
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
                    f"Event {webhook_event.id}: превышено максимальное количество попыток "
                    f"({WebhookEvent.MAX_ATTEMPTS}), дальнейшие ретраи невозможны",
                    level=messages.WARNING,
                )
                logger.warning(
                    f"Webhook {webhook_event.id} ({webhook_event.event_type}) "
                    f"reached max attempts ({WebhookEvent.MAX_ATTEMPTS})"
                )
                continue

            try:
                WebhookService.reprocess_failed(webhook_event.event_id)
                retried += 1
                self.message_user(
                    request,
                    f"Event {webhook_event.id}: переобработка успешна",
                    level=messages.SUCCESS,
                )
                logger.info(
                    f"Webhook {webhook_event.id} ({webhook_event.event_type}) "
                    f"reprocessed successfully"
                )
            except ValueError as e:
                failed_to_retry += 1
                self.message_user(
                    request,
                    f"Event {webhook_event.id}: {str(e)}",
                    level=messages.WARNING,
                )
                logger.warning(f"Webhook {webhook_event.id} reprocess error: {e}")
            except Exception:
                failed_to_retry += 1
                self.message_user(
                    request,
                    f"Event {webhook_event.id}: ошибка при переобработке (см. логи сервера)",
                    level=messages.ERROR,
                )
                logger.exception(f"Webhook {webhook_event.id} reprocess failed")

        if retried:
            self.message_user(
                request,
                f"✓ Переобработано событий: {retried}",
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f"⊘ Пропущено событий (превышен лимит): {skipped}",
                level=messages.WARNING,
            )
        if failed_to_retry:
            self.message_user(
                request,
                f"✗ Ошибок при переобработке: {failed_to_retry}",
                level=messages.ERROR,
            )