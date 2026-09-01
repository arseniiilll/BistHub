from rest_framework import mixins, viewsets, permissions, status
from rest_framework.response import Response

from .models import Order
from .serializers import OrderSerializer, OrderCreateSerializer


class OrderViewSet(mixins.ListModelMixin,
                    mixins.RetrieveModelMixin,
                    mixins.CreateModelMixin,
                    viewsets.GenericViewSet):
    """Заказы пользователя. Только список/детали/создание."""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Самовосстановление старых записей: до исправления payment flow
        # успешный Payment мог сохраниться как succeeded, а связанный Order
        # остаться pending. При чтении заказов приводим такие записи в
        # согласованное состояние. Уже processing/shipped/delivered/canceled
        # заказы не затрагиваются.
        Order.objects.filter(
            user=self.request.user,
            status=Order.StatusChoices.PENDING,
            payments__status__in=['succeeded', 'partially_refunded', 'refunded'],
        ).update(status=Order.StatusChoices.PROCESSING)

        return Order.objects.filter(
            user=self.request.user
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
