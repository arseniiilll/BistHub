from django.db import transaction
from rest_framework import viewsets, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.main.models import Tobacco
from .models import Cart, CartItem
from apps.permissions import IsOfLegalAge
from .serializers import CartSerializer, CartItemSerializer


class CartView(APIView):
    """GET /api/cart/ — вернуть (или создать, если ещё нет) корзину текущего юзера."""
    permission_classes = [permissions.IsAuthenticated, IsOfLegalAge]

    def get(self, request):
        cart, _ = Cart.objects.get_or_create(user=request.user)
        # prefetch_related закрывает N+1: без него total_price/total_items
        # и сериализация items каждый раз заново ходят в БД за self.items.all(),
        # а на каждый item — ещё и за product.
        cart = Cart.objects.select_related('user').prefetch_related('items__product').get(pk=cart.pk)
        serializer = CartSerializer(cart, context={'request': request})
        return Response(serializer.data)


class CartItemViewSet(viewsets.ModelViewSet):
    """Позиции в корзине текущего пользователя.
    /api/cart-items/ — POST добавить, PATCH изменить количество, DELETE убрать."""
    serializer_class = CartItemSerializer
    # IsOfLegalAge закрывает доступ к корзине целиком, включая тех,
    # у кого date_of_birth не указан вовсе — см. User.is_of_legal_age.
    # Проверка нужна именно здесь, на входе в корзину, а не только на чекауте.
    permission_classes = [permissions.IsAuthenticated, IsOfLegalAge]
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        return CartItem.objects.filter(cart__user=self.request.user).select_related('product')

    def perform_create(self, serializer):
        product = serializer.validated_data['product']
        quantity_to_add = serializer.validated_data.get('quantity', 1)

        with transaction.atomic():
            cart, _ = Cart.objects.get_or_create(user=self.request.user)

            # Блокируем строку товара на время транзакции: пока мы здесь,
            # параллельный запрос на тот же product будет ждать снятия лока,
            # а не читать устаревший stock_quantity и проходить проверку мимо нас.
            locked_product = Tobacco.objects.select_for_update().get(pk=product.pk)

            # is_available мог измениться между валидацией сериализатора и
            # получением лока — перепроверяем актуальное значение под локом,
            # а не то, что видел сериализатор до входа в транзакцию.
            if not locked_product.is_available:
                raise ValidationError('Товар недоступен для заказа.')

            existing = CartItem.objects.select_for_update().filter(
                cart=cart, product=locked_product
            ).first()

            if existing:
                new_qty = existing.quantity + quantity_to_add
                if new_qty > locked_product.stock_quantity:
                    raise ValidationError(f'На складе доступно только {locked_product.stock_quantity} шт.')
                existing.quantity = new_qty
                existing.save(update_fields=['quantity'])
                serializer.instance = existing
                self._merged = True
            else:
                if quantity_to_add > locked_product.stock_quantity:
                    raise ValidationError(f'На складе доступно только {locked_product.stock_quantity} шт.')
                serializer.save(cart=cart)
                self._merged = False

    def create(self, request, *args, **kwargs):
        # POST на уже лежащий в корзине товар не создаёт новую позицию,
        # а увеличивает quantity существующей (см. perform_create) —
        # ответ в этом случае должен быть 200, а не вводящий в заблуждение 201.
        response = super().create(request, *args, **kwargs)
        if getattr(self, '_merged', False):
            response.status_code = status.HTTP_200_OK
        return response

    def perform_update(self, serializer):
        """PATCH меняет quantity — та же гонка, что и при создании,
        поэтому финальная проверка остатка тоже должна быть под локом.

        product на update неизменяем (см. CartItemSerializer.validate),
        так что instance.product_id — это тот же товар, что видел клиент."""
        instance = serializer.instance
        quantity = serializer.validated_data.get('quantity', instance.quantity)

        with transaction.atomic():
            try:
                locked_product = Tobacco.objects.select_for_update().get(pk=instance.product_id)
            except Tobacco.DoesNotExist:
                # Товар удалили, пока запрос ждал лок/шёл по сети.
                raise ValidationError('Этот товар больше недоступен.')
            if not locked_product.is_available:
                raise ValidationError('Этот товар больше недоступен.')
            if quantity > locked_product.stock_quantity:
                raise ValidationError(f'На складе доступно только {locked_product.stock_quantity} шт.')
            serializer.save()