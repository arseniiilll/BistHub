# -*- coding: utf-8 -*-
import json
import logging
from decimal import Decimal, ROUND_HALF_UP

import stripe
from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.urls import reverse

from .models import Payment, PaymentAttempt, Refund, WebhookEvent

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

STRIPE_TO_LOCAL_REFUND_STATUS = {
    'succeeded': 'succeeded',
    'pending': 'pending',
    'failed': 'failed',
    'canceled': 'cancelled',
}


def _stripe_obj_to_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return json.loads(str(obj))


def _get(obj, key, default=None):
    if obj is None:
        return default
    return obj[key] if key in obj else default


def to_minor_units(amount: Decimal) -> int:
    minor_units = int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if minor_units <= 0:
        raise ValueError(f"Сумма должна быть не менее 0.01, получено {amount}")
    return minor_units


class PaymentService:
    """Создание и возобновление Stripe Checkout Session для заказа."""

    @staticmethod
    def _retrieve_existing_session(payment):
        if not payment.stripe_session_id:
            return None
        try:
            return stripe.checkout.Session.retrieve(payment.stripe_session_id)
        except stripe.error.InvalidRequestError:
            return None

    @staticmethod
    def create_checkout_session(order, request):
        with transaction.atomic():
            order_locked = order.__class__.objects.select_for_update().get(pk=order.pk)

            if order_locked.user_id != request.user.id:
                raise ValueError("Это не ваш заказ.")

            existing = Payment.objects.select_for_update().filter(
                order=order_locked,
                status__in=[
                    'pending', 'processing',
                    'succeeded', 'partially_refunded', 'refunded',
                ],
            ).order_by('-created_at').first()

            if existing and existing.status in ('succeeded', 'partially_refunded', 'refunded'):
                raise ValueError("Заказ уже оплачен.")

            items = list(order_locked.items.select_related('tobacco').all())
            if not items:
                raise ValueError("Нельзя оплатить пустой заказ.")

            items_total = sum((item.price * item.quantity for item in items), Decimal('0'))
            if items_total != order_locked.total_price:
                raise ValueError(
                    "Сумма заказа не совпадает с суммой позиций "
                    f"({order_locked.total_price} != {items_total})."
                )
            to_minor_units(items_total)

        # Сетевой вызов Stripe делаем вне DB-транзакции.
        if existing and existing.status in ('pending', 'processing'):
            session = PaymentService._retrieve_existing_session(existing)
            if session is not None:
                session_status = _get(session, 'status')
                payment_status = _get(session, 'payment_status')

                if session_status == 'open' and _get(session, 'url'):
                    return {
                        'checkout_url': session.url,
                        'session_id': session.id,
                        'payment_id': existing.id,
                    }

                if session_status == 'complete' and payment_status == 'paid':
                    with transaction.atomic():
                        payment = Payment.objects.select_for_update().get(pk=existing.pk)
                        if payment.status not in ('succeeded', 'partially_refunded', 'refunded'):
                            payment.stripe_payment_intent_id = _get(session, 'payment_intent')
                            payment.mark_as_succeeded()
                            order_to_update = order.__class__.objects.select_for_update().get(pk=order.pk)
                            pending_value = getattr(order_to_update.StatusChoices, 'PENDING', 'pending')
                            processing_value = getattr(order_to_update.StatusChoices, 'PROCESSING', 'processing')
                            if order_to_update.status == pending_value:
                                order_to_update.status = processing_value
                                order_to_update.save(update_fields=['status', 'updated'])
                    raise ValueError("Заказ уже оплачен.")

                if session_status == 'expired':
                    with transaction.atomic():
                        payment = Payment.objects.select_for_update().get(pk=existing.pk)
                        if payment.status in ('pending', 'processing'):
                            payment.mark_as_cancelled()
                elif session_status == 'complete' and payment_status != 'paid':
                    with transaction.atomic():
                        payment = Payment.objects.select_for_update().get(pk=existing.pk)
                        if payment.status in ('pending', 'processing'):
                            payment.mark_as_failed(reason='Stripe Checkout completed without successful payment.')
                else:
                    raise ValueError("Оплата ещё обрабатывается. Попробуйте снова через несколько секунд.")
            else:
                # Локальная запись есть, а Stripe Session нет/недоступна — не держим её вечно активной.
                with transaction.atomic():
                    payment = Payment.objects.select_for_update().get(pk=existing.pk)
                    if payment.status in ('pending', 'processing'):
                        payment.mark_as_cancelled()

        # Создаём новый Payment только когда старой активной сессии уже нет.
        with transaction.atomic():
            order_locked = order.__class__.objects.select_for_update().get(pk=order.pk)
            still_active = Payment.objects.filter(
                order=order_locked,
                status__in=['pending', 'processing', 'succeeded', 'partially_refunded', 'refunded'],
            ).exists()
            if still_active:
                raise ValueError("Не удалось подготовить повторную оплату. Обновите страницу и попробуйте ещё раз.")

            payment = Payment.objects.create(
                user=request.user,
                order=order_locked,
                amount=items_total,
                currency=Payment.DEFAULT_CURRENCY,
                status='pending',
                payment_method=Payment.PaymentProvider.STRIPE,
                description=f"Payment for Order #{order_locked.id}",
            )

        try:
            success_url = request.build_absolute_uri(
                reverse('payment:payment-success', args=[payment.id])
            )
            cancel_url = request.build_absolute_uri(
                reverse('payment:payment-cancel', args=[payment.id])
            )

            line_items = [
                {
                    'price_data': {
                        'currency': payment.currency.lower(),
                        'unit_amount': to_minor_units(item.price),
                        'product_data': {'name': item.tobacco.name},
                    },
                    'quantity': item.quantity,
                }
                for item in items
            ]

            checkout_metadata = {
                'payment_id': str(payment.id),
                'order_id': str(order.id),
                'user_id': str(request.user.id),
            }

            session_kwargs = dict(
                payment_method_types=['card'],
                line_items=line_items,
                mode='payment',
                success_url=success_url,
                cancel_url=cancel_url,
                metadata=checkout_metadata,
                payment_intent_data={'metadata': checkout_metadata},
                idempotency_key=f"checkout-session-{payment.id}",
            )

            customer_email = getattr(order_locked, 'email', None)
            if customer_email:
                session_kwargs['customer_email'] = customer_email

            session = stripe.checkout.Session.create(**session_kwargs)
            payment.stripe_session_id = session.id
            payment.status = 'processing'
            payment.save(update_fields=['stripe_session_id', 'status', 'updated_at'])

            return {
                'checkout_url': session.url,
                'session_id': session.id,
                'payment_id': payment.id,
            }

        except stripe.error.StripeError as e:
            payment.mark_as_failed(reason=str(e))
            PaymentAttempt.objects.create(
                payment=payment,
                status=PaymentAttempt.Status.FAILED,
                error_message=str(e),
            )
            raise ValueError(f"Stripe error: {str(e)}")
        except Exception as e:
            payment.mark_as_failed(reason=f"Internal error: {e}")
            PaymentAttempt.objects.create(
                payment=payment,
                status=PaymentAttempt.Status.FAILED,
                error_message=str(e),
            )
            raise


class WebhookService:
    @staticmethod
    def verify_and_parse(payload, sig_header):
        try:
            return stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            raise ValueError(f"Invalid payload: {str(e)}")
        except stripe.error.SignatureVerificationError as e:
            raise ValueError(f"Invalid signature: {str(e)}")

    @staticmethod
    def process_event(event):
        with transaction.atomic():
            try:
                with transaction.atomic():
                    webhook_event, created = WebhookEvent.objects.select_for_update().get_or_create(
                        event_id=event['id'],
                        defaults={
                            'provider': 'stripe',
                            'event_type': event['type'],
                            'data': _stripe_obj_to_dict(event['data']) if 'data' in event else {},
                            'status': 'pending',
                        }
                    )
            except IntegrityError:
                webhook_event = WebhookEvent.objects.select_for_update().get(event_id=event['id'])
                created = False

            if not created and webhook_event.status == 'processed':
                return webhook_event

            if webhook_event.status == 'failed' and not webhook_event.should_retry():
                logger.error(
                    "Webhook event %s (%s) исчерпал лимит попыток (%s)",
                    webhook_event.event_id, webhook_event.event_type,
                    WebhookEvent.MAX_ATTEMPTS,
                )
                return webhook_event

            handler = WEBHOOK_HANDLERS.get(event['type'])
            if not handler:
                webhook_event.status = 'ignored'
                webhook_event.save(update_fields=['status'])
                return webhook_event

            webhook_event.increment_attempts()

        try:
            event_data = event['data'] if 'data' in event else {}
            handler(event_data['object'] if 'object' in event_data else event)
        except Exception as e:
            webhook_event.mark_as_failed(str(e))
            raise
        else:
            webhook_event.mark_as_processed()

        return webhook_event

    @staticmethod
    def reprocess_failed(event_id):
        with transaction.atomic():
            webhook_event = WebhookEvent.objects.select_for_update().get(event_id=event_id)
            if webhook_event.status == 'processed':
                return webhook_event
            if not webhook_event.should_retry():
                raise ValueError(
                    f"Событие {event_id} превысило максимум попыток ({WebhookEvent.MAX_ATTEMPTS})."
                )
            handler = WEBHOOK_HANDLERS.get(webhook_event.event_type)
            if not handler:
                webhook_event.status = 'ignored'
                webhook_event.save(update_fields=['status'])
                return webhook_event
            webhook_event.increment_attempts()
            event_data = webhook_event.data

        try:
            handler(event_data.get('object', event_data))
        except Exception as e:
            webhook_event.mark_as_failed(str(e))
            raise
        else:
            webhook_event.mark_as_processed()
        return webhook_event


def _handle_checkout_session_completed(session):
    metadata = _get(session, 'metadata') or {}
    payment_id = _get(metadata, 'payment_id')
    if not payment_id:
        return

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().select_related('order').get(id=payment_id)
        except Payment.DoesNotExist:
            return

        if payment.status != 'succeeded':
            payment.stripe_payment_intent_id = _get(session, 'payment_intent')
            payment.mark_as_succeeded()

        order = payment.order.__class__.objects.select_for_update().get(pk=payment.order_id)
        pending_value = getattr(order.StatusChoices, 'PENDING', 'pending')
        processing_value = getattr(order.StatusChoices, 'PROCESSING', 'processing')
        if order.status == pending_value:
            order.status = processing_value
            order.save(update_fields=['status', 'updated'])

        attempt, created = PaymentAttempt.objects.get_or_create(
            payment=payment,
            stripe_payment_intent_id=_get(session, 'payment_intent'),
            defaults={'status': PaymentAttempt.Status.SUCCEEDED}
        )
        if not created and attempt.status != PaymentAttempt.Status.SUCCEEDED:
            attempt.status = PaymentAttempt.Status.SUCCEEDED
            attempt.save(update_fields=['status'])


def _handle_checkout_session_expired(session):
    metadata = _get(session, 'metadata') or {}
    payment_id = _get(metadata, 'payment_id')
    if not payment_id:
        return
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(id=payment_id)
        except Payment.DoesNotExist:
            return
        if payment.status in ('pending', 'processing'):
            payment.mark_as_cancelled()


def _handle_payment_intent_payment_failed(payment_intent):
    stripe_pi_id = _get(payment_intent, 'id')
    with transaction.atomic():
        payment = None
        metadata = _get(payment_intent, 'metadata') or {}
        payment_id = _get(metadata, 'payment_id')
        if payment_id:
            try:
                payment = Payment.objects.select_for_update().get(id=payment_id)
            except Payment.DoesNotExist:
                pass
        if payment is None and stripe_pi_id:
            try:
                payment = Payment.objects.select_for_update().get(
                    stripe_payment_intent_id=stripe_pi_id
                )
            except Payment.DoesNotExist:
                pass
        if payment is None:
            return
        if payment.status in ('succeeded', 'failed', 'cancelled', 'refunded', 'partially_refunded'):
            return
        if stripe_pi_id and not payment.stripe_payment_intent_id:
            payment.stripe_payment_intent_id = stripe_pi_id
            payment.save(update_fields=['stripe_payment_intent_id', 'updated_at'])
        error = _get(payment_intent, 'last_payment_error') or {}
        reason = _get(error, 'message', 'Unknown error')
        attempt, created = PaymentAttempt.objects.get_or_create(
            payment=payment,
            stripe_payment_intent_id=stripe_pi_id,
            defaults={
                'status': PaymentAttempt.Status.FAILED,
                'error_message': reason,
            }
        )
        if not created and attempt.status != PaymentAttempt.Status.SUCCEEDED:
            attempt.status = PaymentAttempt.Status.FAILED
            attempt.error_message = reason
            attempt.save(update_fields=['status', 'error_message'])


def _handle_checkout_session_async_payment_failed(session):
    metadata = _get(session, 'metadata') or {}
    payment_id = _get(metadata, 'payment_id')
    if not payment_id:
        return
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(id=payment_id)
        except Payment.DoesNotExist:
            return
        if payment.status in ('pending', 'processing'):
            payment.mark_as_failed(
                reason=f"Асинхронный отказ платежа (статус: {_get(session, 'status', 'unknown')})"
            )


def _handle_charge_refunded(charge):
    payment_intent_id = _get(charge, 'payment_intent')
    if not payment_intent_id:
        return
    refunds_data = _get(charge, 'refunds') or {}
    refund_list = list(_get(refunds_data, 'data', []))
    if _get(refunds_data, 'has_more'):
        try:
            refund_list = list(stripe.Refund.list(
                payment_intent=payment_intent_id,
                limit=100
            ).auto_paging_iter())
        except stripe.error.StripeError:
            pass

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(
                stripe_payment_intent_id=payment_intent_id
            )
        except Payment.DoesNotExist:
            return

        for stripe_refund in refund_list:
            stripe_refund_id = _get(stripe_refund, 'id')
            if not stripe_refund_id:
                continue
            local_status = STRIPE_TO_LOCAL_REFUND_STATUS.get(
                _get(stripe_refund, 'status'), 'pending'
            )
            refund_amount = Decimal(_get(stripe_refund, 'amount', 0)) / 100

            refund, created = Refund.objects.get_or_create(
                stripe_refund_id=stripe_refund_id,
                defaults={
                    'payment': payment,
                    'amount': refund_amount,
                    'reason': 'Возврат оформлен через Stripe.',
                    'status': local_status,
                }
            )
            if not created and refund.status != local_status:
                refund.status = local_status
                refund.save(update_fields=['status'])

        total_refunded = payment.refunds.filter(status='succeeded').aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0')
        if total_refunded >= payment.amount:
            payment.mark_as_fully_refunded()
        elif total_refunded > Decimal('0'):
            payment.mark_as_partially_refunded()


WEBHOOK_HANDLERS = {
    'checkout.session.completed': _handle_checkout_session_completed,
    'checkout.session.expired': _handle_checkout_session_expired,
    'checkout.session.async_payment_failed': _handle_checkout_session_async_payment_failed,
    'payment_intent.payment_failed': _handle_payment_intent_payment_failed,
    'charge.refunded': _handle_charge_refunded,
}
