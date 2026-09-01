# -*- coding: utf-8 -*-
import logging

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Payment
from .serializers import PaymentCreateSerializer, PaymentSerializer
from .services import PaymentService, WebhookService

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


class PaymentViewSet(mixins.ListModelMixin,
                     mixins.RetrieveModelMixin,
                     mixins.CreateModelMixin,
                     viewsets.GenericViewSet):
    """Платежи текущего пользователя и создание Stripe Checkout Session."""

    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Payment.objects.filter(user=self.request.user).select_related('order')

    def get_serializer_class(self):
        return PaymentCreateSerializer if self.action == 'create' else PaymentSerializer

    def get_serializer_context(self):
        return {'request': self.request}

    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny])
    def success(self, request, pk=None):
        """Synchronise a completed Stripe session, then return to React."""
        payment = get_object_or_404(
            Payment.objects.select_related('order'),
            pk=pk,
        )

        if payment.stripe_session_id:
            try:
                session = stripe.checkout.Session.retrieve(payment.stripe_session_id)
                session_status = getattr(session, 'status', None)
                payment_status = getattr(session, 'payment_status', None)

                if session_status == 'complete' and payment_status == 'paid':
                    with transaction.atomic():
                        locked_payment = (
                            Payment.objects
                            .select_for_update()
                            .get(pk=payment.pk)
                        )
                        if locked_payment.status not in (
                            Payment.Status.SUCCEEDED,
                            Payment.Status.PARTIALLY_REFUNDED,
                            Payment.Status.REFUNDED,
                        ):
                            payment_intent_id = getattr(session, 'payment_intent', None)
                            if payment_intent_id and not locked_payment.stripe_payment_intent_id:
                                locked_payment.stripe_payment_intent_id = payment_intent_id
                            # mark_as_succeeded() also advances a pending Order to processing.
                            locked_payment.mark_as_succeeded()
            except stripe.error.StripeError:
                # The webhook remains the source of truth; a temporary Stripe API
                # failure must not strand the browser on the backend callback.
                logger.exception(
                    'Could not synchronise Stripe session for payment %s on success redirect',
                    payment.id,
                )

        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:5173').rstrip('/')
        return redirect(
            f'{frontend_url}/payment/success?payment_id={payment.id}&order_id={payment.order_id}'
        )

    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny])
    def cancel(self, request, pk=None):
        """Return the browser to the React payment-cancel page."""
        payment = get_object_or_404(
            Payment.objects.select_related('order').only('id', 'order_id'),
            pk=pk,
        )
        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:5173').rstrip('/')
        return redirect(
            f'{frontend_url}/payment/cancel?payment_id={payment.id}&order_id={payment.order_id}'
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.validated_data['order']

        try:
            result = PaymentService.create_checkout_session(order, request)
        except ValueError as exc:
            logger.warning(
                'ValueError creating payment for order %s: %s',
                order.id,
                exc,
            )
            return Response(
                {'non_field_errors': [str(exc)]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IntegrityError as exc:
            logger.error(
                'IntegrityError creating payment for order %s: %s',
                order.id,
                exc,
            )
            return Response(
                {'non_field_errors': [
                    'Ошибка при создании платежа. Пожалуйста, попробуйте ещё раз.'
                ]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception('Unexpected error creating payment for order %s', order.id)
            return Response(
                {'non_field_errors': ['Внутренняя ошибка сервера. Попробуйте ещё раз позже.']},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            payment = Payment.objects.select_related('order').get(id=result['payment_id'])
        except Payment.DoesNotExist:
            logger.error(
                'Payment %s created but not found afterwards',
                result['payment_id'],
            )
            return Response(
                {'non_field_errors': ['Ошибка при создании платежа. Пожалуйста, попробуйте ещё раз.']},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        output_serializer = PaymentSerializer(
            payment,
            context=self.get_serializer_context(),
        )
        return Response(
            {**output_serializer.data, 'checkout_url': result['checkout_url']},
            status=status.HTTP_201_CREATED,
        )


class StripeWebhookView(APIView):
    """Receive verified Stripe webhook events."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')

        if not sig_header:
            logger.warning('Webhook received without Stripe-Signature header')
            return Response(
                {'detail': 'Missing Stripe-Signature header.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            event = WebhookService.verify_and_parse(payload, sig_header)
        except ValueError as exc:
            logger.warning('Invalid Stripe webhook signature or payload: %s', exc)
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            WebhookService.process_event(event)
        except Exception as exc:
            logger.exception(
                'Failed to process Stripe webhook event %s (type: %s): %s',
                getattr(event, 'id', None),
                getattr(event, 'type', None),
                exc,
            )
            # Important: a 5xx tells Stripe delivery failed and should be retried.
            return Response(
                {'detail': 'Webhook processing failed.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(status=status.HTTP_200_OK)
