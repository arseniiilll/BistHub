from rest_framework import mixins, viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Order
from .serializers import OrderSerializer, OrderCreateSerializer


class OrderViewSet(mixins.ListModelMixin,
                    mixins.RetrieveModelMixin,
                    mixins.CreateModelMixin,
                    mixins.DestroyModelMixin,
                    viewsets.GenericViewSet):
    """Заказы пользователя: список, детали, создание и скрытие из истории."""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Самовосстановление старых записей: до исправления payment flow
        # успешный Payment мог сохраниться как succeeded, а связанный Order
        # остаться pending.
        Order.objects.filter(
            user=self.request.user,
            status=Order.StatusChoices.PENDING,
            payments__status__in=['succeeded', 'partially_refunded', 'refunded'],
        ).update(status=Order.StatusChoices.PROCESSING)

        return Order.objects.filter(
            user=self.request.user,
            hidden_from_history=False,
        ).prefetch_related('items__tobacco')

    def get_serializer_class(self):
        return OrderCreateSerializer if self.action == 'create' else OrderSerializer

    def get_serializer_context(self):
        return {'request': self.request}

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()

        output_serializer = OrderSerializer(
            order,
            context=self.get_serializer_context()
        )
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # Не удаляем финансовую/Stripe историю каскадом. Для пользователя
        # заказ исчезает из истории, но остаётся доступным администратору.
        instance.hidden_from_history = True
        instance.save(update_fields=['hidden_from_history', 'updated'])

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        ids = request.data.get('ids', [])
        if not isinstance(ids, list) or not ids:
            return Response(
                {'detail': 'Передайте непустой список ids.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            ids = [int(order_id) for order_id in ids]
        except (TypeError, ValueError):
            return Response(
                {'detail': 'Все ids должны быть числами.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = Order.objects.filter(
            user=request.user,
            id__in=ids,
            hidden_from_history=False,
        )
        count = queryset.update(hidden_from_history=True)
        return Response({'deleted': count}, status=status.HTTP_200_OK)
