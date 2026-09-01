# -*- coding: utf-8 -*-
import logging

from rest_framework import mixins, viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db import IntegrityError

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

    Все финансовые операции выполняются только через PaymentService,
    который использует select_for_update() для предотвращения race conditions.

    Endpoints:
    - GET /api/payments/ — список платежей текущего пользователя
    - GET /api/payments/{id}/ — детали платежа
    - POST /api/payments/ — инициировать новый платёж (создаёт Stripe Checkout Session)
    - GET /api/payments/{id}/success/ — redirect URL после успешной оплаты
    - GET /api/payments/{id}/cancel/ — redirect URL при отмене оплаты
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Платежи только текущего пользователя, с предзагруженным заказом."""
        return Payment.objects.filter(user=self.request.user).select_related('order')

    def get_serializer_class(self):
        return PaymentCreateSerializer if self.action == 'create' else PaymentSerializer

    def get_serializer_context(self):
        return {'request': self.request}

    @action(detail=True, methods=['get'])
    def success(self, request, pk=None):
        """
        Обработка успешной оплаты (redirect от Stripe).

        ВАЖНО: Реальный статус платежа подтверждается только вебхуком
        (checkout.session.completed), этот эндпоинт лишь показывает
        пользователю текущее состояние Payment.
        """
        payment = self.get_object()
        serializer = self.get_serializer(payment)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def cancel(self, request, pk=None):
        """
        Обработка отмены оплаты (redirect от Stripe).

        ВАЖНО: Реальный статус платежа подтверждается только вебхуком,
        этот эндпоинт лишь показывает пользователю текущее состояние Payment.
        """
        payment = self.get_object()
        serializer = self.get_serializer(payment)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        """
        Инициировать новый платёж.

        Процесс:
        1. Валидируем заказ через PaymentCreateSerializer
        2. Вызываем PaymentService.create_checkout_session(order, request)
        3. Возвращаем Payment + checkout_url для редиректа на оплату

        Статус коды:
        - 201 CREATED: платёж успешно инициирован
        - 400 BAD REQUEST: ошибка валидации или бизнес-логики
        - 500 INTERNAL SERVER ERROR: непредвиденная ошибка
        """
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
    """
    Приём событий от Stripe. Проверяет подпись, сохраняет событие
    (идемпотентно по event_id) и вызывает соответствующий обработчик.

    АРХИТЕКТУРА ВЕБХУКОВ:
    1. Stripe отправляет POST-запрос с подписью (HMAC-SHA256)
    2. Мы проверяем подпись через WebhookService.verify_and_parse()
    3. Сохраняем событие в БД (WebhookEvent) с статусом 'pending'
    4. Обрабатываем событие через WebhookService.process_event()
    5. Всегда возвращаем 200 OK (сигнализируем Stripe получение события)
    """
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        """
        Обработать вебхук от Stripe.

        Возвращает:
            Response с статусом 200 (всегда) или 400 (только для неверной подписи)
        """
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