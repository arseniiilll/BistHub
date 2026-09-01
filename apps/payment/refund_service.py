# -*- coding: utf-8 -*-
"""Stripe refund service kept separate from checkout/webhook services."""

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from .models import Payment, Refund

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


def _to_minor_units(amount: Decimal) -> int:
    value = int((Decimal(amount) * 100).quantize(Decimal('1')))
    if value <= 0:
        raise ValueError('Сумма возврата должна быть больше нуля.')
    return value


class RefundService:
    """Создание возврата средств через Stripe с блокировкой платежа."""

    @staticmethod
    def create_refund(payment, amount, reason='', created_by=None):
        amount = Decimal(amount)

        with transaction.atomic():
            payment_locked = Payment.objects.select_for_update().get(pk=payment.pk)

            if not payment_locked.can_be_refunded:
                raise ValueError(
                    f'Этот платёж нельзя вернуть. Статус: {payment_locked.status}, '
                    f'метод: {payment_locked.payment_method}'
                )

            if not payment_locked.stripe_payment_intent_id:
                raise ValueError('У платежа отсутствует Stripe PaymentIntent ID.')

            already_reserved = payment_locked.refunds.filter(
                status__in=['succeeded', 'pending']
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            if amount <= 0:
                raise ValueError('Сумма возврата должна быть больше нуля.')

            if already_reserved + amount > payment_locked.amount:
                available = payment_locked.amount - already_reserved
                raise ValueError(
                    f'Сумма возврата превышает доступный остаток. '
                    f'Доступно: {available} {payment_locked.currency}.'
                )

            refund_record = Refund.objects.create(
                payment=payment_locked,
                amount=amount,
                reason=reason,
                created_by=created_by,
                status='pending',
            )

        try:
            stripe_refund = stripe.Refund.create(
                payment_intent=payment_locked.stripe_payment_intent_id,
                amount=_to_minor_units(amount),
                reason='requested_by_customer',
                metadata={
                    'refund_id': str(refund_record.id),
                    'payment_id': str(payment_locked.id),
                },
                idempotency_key=f'refund-{refund_record.id}',
            )
        except stripe.error.StripeError as exc:
            refund_record.mark_as_failed(reason=str(exc))
            raise ValueError(f'Stripe refund error: {exc}') from exc
        except Exception as exc:
            refund_record.mark_as_failed(reason=str(exc))
            raise ValueError(f'Refund creation error: {exc}') from exc

        refund_record.stripe_refund_id = stripe_refund.id
        refund_record.save(update_fields=['stripe_refund_id'])

        stripe_status = getattr(stripe_refund, 'status', None)
        if stripe_status == 'failed':
            refund_record.mark_as_failed(reason='Stripe marked refund as failed.')
            raise ValueError('Stripe отклонил возврат.')

        if stripe_status == 'succeeded':
            refund_record.mark_as_succeeded()

            with transaction.atomic():
                payment_locked = Payment.objects.select_for_update().get(pk=payment_locked.pk)
                total_refunded = payment_locked.refunds.filter(
                    status='succeeded'
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

                if total_refunded >= payment_locked.amount:
                    payment_locked.mark_as_fully_refunded()
                elif total_refunded > Decimal('0'):
                    payment_locked.mark_as_partially_refunded()

        logger.info(
            'Refund %s created for payment %s (Stripe %s)',
            refund_record.id,
            payment_locked.id,
            stripe_refund.id,
        )
        return refund_record
