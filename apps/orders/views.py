from rest_framework import mixins, viewsets, permissions, status
from rest_framework.response import Response

from .models import Order
from .serializers import OrderSerializer, OrderCreateSerializer


class OrderViewSet(mixins.ListModelMixin,
                    mixins.RetrieveModelMixin,
                    mixins.CreateModelMixin,
                    viewsets.GenericViewSet):
    """Заказы пользователя. Только список/детали/создание —
    статус и сумма меняются внутренней логикой (оплата, вебхуки, админка),
    поэтому update/delete через API намеренно не предоставлены."""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).prefetch_related('items__tobacco')

    def get_serializer_class(self):
        return OrderCreateSerializer if self.action == 'create' else OrderSerializer

    def get_serializer_context(self):
        return {'request': self.request}

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()

        output_serializer = OrderSerializer(order, context=self.get_serializer_context())
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)