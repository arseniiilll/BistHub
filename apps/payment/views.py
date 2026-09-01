# -*- coding: utf-8 -*-
import logging

import stripe
from rest_framework import mixins, viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect

from .models import Payment
from .serializers import PaymentSerializer, PaymentCreateSerializer
from .services import PaymentService, WebhookService

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


class PaymentViewSet(mixins.ListModelMixin,
                     mixins.RetrieveModelMixin,
                     mixins.CreateModelMixin,
                     viewsets.GenericViewSet):
    """
    Платежи пользователя. Создание инициирует Stripe Checkout Session.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Payment.objects.filter(user=self.request.user).select_related('order')

    def get_serializer_class(self):
        return PaymentCreateSerializer if self.action == 'create' else PaymentSerializer

    def get_serializer_context(self):
        return {'request': self.request}

    @action(detail=True, methods=['get'], permission_classes=[permissions.AllowAny])
    def success(self, request, pk=None):
        """
        Stripe возвращает браузер сюда после успешного Checkout.

        Webhook остаётся главным источником истины, но для UX мы дополнительно
        синхронизируем Stripe Checkout Session прямо здесь. Поэтому к моменту
        redirect на React заказ уже не остаётся визуально в PENDING, даже если
        webhook пришёл на долю секунды позже.
        """
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
                            .select_related('order')
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
                            locked_payment.mark_as_succeeded()

                        order = (
                            locked_payment.order.__class__.objects
                            .select_for_update()
                            .get(pk=locked_payment.order_id)
                        )
                        if order.status == order.StatusChoices.PENDING:
                            order.status = order.StatusChoices.PROCESSING
                            order.save(update_fields=['status', 'updated'])

            except stripe.error.StripeError:
                # Не ломаем redirect пользователю, если Stripe API временно
                # недоступен: webhook всё равно остаётся резервным механизмом.
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
        """Return the browser to a dedicated React payment-cancel page."""
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
        except ValueError as e:
            logger.warning(
                f"ValueError creating payment for order {order.id}: {str(e)}"
            )
            return Response(
                {'non_field_errors': [str(e)]},
                status=status.HTTP_400_BAD_REQUEST
            )
        except IntegrityError as e:
            logger.error(
                f"IntegrityError creating payment for order {order.id}: {str(e)}"
            )
            return Response(
                {'non_field_errors': [
                    'Ошибка при создании платежа. Пожалуйста, попробуйте ещё раз.'
                ]},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception:
            logger.exception(
                f"Unexpected error creating payment for order {order.id}"
            )
            return Response(
                {'non_field_errors': ['Внутренняя ошибка сервера. Попробуйте ещё раз позже.']},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            payment = Payment.objects.get(id=result['payment_id'])
        except Payment.DoesNotExist:
            logger.error(
                f"Payment {result['payment_id']} created but not found afterwards"
            )
            return Response(
                {'non_field_errors': ['Ошибка при создании платежа. Пожалуйста, попробуйте ещё раз.']},
                status=status.HTTP_400_BAD_REQUEST
            )

        output_serializer = PaymentSerializer(
            payment,
            context=self.get_serializer_context()
        )

        return Response(
            {**output_serializer.data, 'checkout_url': result['checkout_url']},
            status=status.HTTP_201_CREATED,
        )


class StripeWebhookView(APIView):
    """Приём и обработка Stripe webhook events."""
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')

        if not sig_header:
            logger.warning("Webhook received without Stripe-Signature header")
            return Response(
                {'detail': 'Missing Stripe-Signature header.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            event = WebhookService.verify_and_parse(payload, sig_header)
        except ValueError as e:
            logger.warning(f"Invalid Stripe webhook signature or payload: {str(e)}")
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            WebhookService.process_event(event)
        except Exception as e:
            logger.exception(
                "Failed to process Stripe webhook event %s (type: %s): %s",
                getattr(event, 'id', None),
                getattr(event, 'type', None),
                str(e)
            )

        return Response(status=status.HTTP_200_OK)
