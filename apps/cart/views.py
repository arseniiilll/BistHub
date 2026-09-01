from django.db import transaction
from django.db.models import prefetch_related_objects
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Tobacco
from apps.permissions import IsOfLegalAge
from .models import Cart, CartItem
from .serializers import CartItemSerializer, CartSerializer


class CartView(APIView):
    """GET /api/cart/ — вернуть (или создать) корзину текущего пользователя."""
    permission_classes = [permissions.IsAuthenticated, IsOfLegalAge]

    def get(self, request):
        cart, _ = Cart.objects.get_or_create(user=request.user)
        # Не делаем второй SELECT самой корзины после get_or_create().
        # Подгружаем только связанные позиции + товары в уже полученный объект.
        prefetch_related_objects([cart], 'items__product')
        serializer = CartSerializer(cart, context={'request': request})
        return Response(serializer.data)


class CartItemViewSet(viewsets.ModelViewSet):
    """Позиции корзины текущего пользователя."""
    serializer_class = CartItemSerializer
    permission_classes = [permissions.IsAuthenticated, IsOfLegalAge]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        return CartItem.objects.filter(cart__user=self.request.user).select_related('product')

    def perform_create(self, serializer):
        product = serializer.validated_data['product']
        quantity_to_add = serializer.validated_data.get('quantity', 1)

        with transaction.atomic():
            cart, _ = Cart.objects.get_or_create(user=self.request.user)
            locked_product = Tobacco.objects.select_for_update().get(pk=product.pk)

            if not locked_product.is_available:
                raise ValidationError('Товар недоступен для заказа.')

            existing = CartItem.objects.select_for_update().filter(
                cart=cart,
                product=locked_product,
            ).first()

            if existing:
                new_qty = existing.quantity + quantity_to_add
                if new_qty > locked_product.stock_quantity:
                    raise ValidationError(
                        f'На складе доступно только {locked_product.stock_quantity} шт.'
                    )
                existing.quantity = new_qty
                existing.save(update_fields=['quantity'])
                serializer.instance = existing
                self._merged = True
            else:
                if quantity_to_add > locked_product.stock_quantity:
                    raise ValidationError(
                        f'На складе доступно только {locked_product.stock_quantity} шт.'
                    )
                serializer.save(cart=cart)
                self._merged = False

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        if getattr(self, '_merged', False):
            response.status_code = status.HTTP_200_OK
        return response

    def perform_update(self, serializer):
        instance = serializer.instance
        quantity = serializer.validated_data.get('quantity', instance.quantity)

        with transaction.atomic():
            try:
                locked_product = Tobacco.objects.select_for_update().get(
                    pk=instance.product_id
                )
            except Tobacco.DoesNotExist:
                raise ValidationError('Этот товар больше недоступен.')

            if not locked_product.is_available:
                raise ValidationError('Этот товар больше недоступен.')
            if quantity > locked_product.stock_quantity:
                raise ValidationError(
                    f'На складе доступно только {locked_product.stock_quantity} шт.'
                )
            serializer.save()
