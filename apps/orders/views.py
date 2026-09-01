from django.utils import timezone
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
    MAX_BULK_DELETE_IDS = 200

    def get_queryset(self):
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
        if len(ids) > self.MAX_BULK_DELETE_IDS:
            return Response(
                {'detail': f'За один запрос можно скрыть не более {self.MAX_BULK_DELETE_IDS} заказов.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            ids = list(dict.fromkeys(int(order_id) for order_id in ids))
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
        count = queryset.update(
            hidden_from_history=True,
            updated=timezone.now(),
        )
        return Response({'deleted': count}, status=status.HTTP_200_OK)
