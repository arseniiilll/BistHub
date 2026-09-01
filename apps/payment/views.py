# -*- coding: utf-8 -*-
import logging

from rest_framework import mixins, viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from django.conf import settings
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect

from .models import Payment
from .serializers import PaymentSerializer, PaymentCreateSerializer
from .services import PaymentService, WebhookService

logger = logging.getLogger(__name__)


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
        """Return the browser to a dedicated React payment-success page."""
        payment = get_object_or_404(
            Payment.objects.select_related('order').only('id', 'order_id'),
            pk=pk,
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
