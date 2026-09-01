from rest_framework import serializers
from django.db import transaction
from .models import Order, OrderItem
from apps.cart.models import Cart
from apps.main.models import Tobacco


class OrderItemSerializer(serializers.ModelSerializer):
    # product_name — снимок имени на момент покупки. Показываем его, а если он
    # почему-то пуст (например, запись создана через bulk_create(), который
    # обходит OrderItem.save()) — подстраховываемся живым tobacco.name.
    tobacco_name = serializers.SerializerMethodField()
    total_price = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ['id', 'tobacco', 'tobacco_name', 'quantity', 'price', 'total_price']
        read_only_fields = ['id', 'tobacco', 'quantity', 'price', 'total_price']

    def get_tobacco_name(self, obj):
        return obj.product_name or (obj.tobacco.name if obj.tobacco_id else '')

    def get_total_price(self, obj):
        return obj.get_total_price()


class OrderSerializer(serializers.ModelSerializer):
    """Для чтения заказа — все финансовые/статусные поля read-only."""
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'user', 'first_name', 'last_name', 'email',
            'address1', 'address2', 'city', 'country', 'province',
            'postal_code', 'phone', 'total_price', 'status',
            'items', 'created', 'updated',
        ]
        read_only_fields = ['id', 'user', 'total_price', 'status', 'created', 'updated']


class OrderCreateSerializer(serializers.ModelSerializer):
    """Создаёт заказ из корзины текущего пользователя.
    Требует, чтобы вьюха передавала request в context."""

    class Meta:
        model = Order
        fields = [
            'first_name', 'last_name', 'email',
            'address1', 'address2', 'city', 'country',
            'province', 'postal_code', 'phone',
        ]

    def validate(self, attrs):
        request = self.context['request']
        user = request.user

        cart = Cart.objects.filter(user=user).first()
        if not cart:
            raise serializers.ValidationError('Корзина пуста.')

        cart_items = list(cart.items.select_related('product'))
        if not cart_items:
            raise serializers.ValidationError('Корзина пуста.')

        if any(item.product.requires_age_verification for item in cart_items) and not user.is_of_legal_age:
            raise serializers.ValidationError(
                'Для покупки этого товара нужно подтвердить дату рождения '
                'и возраст 18+ в профиле аккаунта.'
            )

        for item in cart_items:
            if not item.product.is_available:
                raise serializers.ValidationError(f'Товар "{item.product.name}" больше не доступен для заказа.')
            if item.quantity > item.product.stock_quantity:
                raise serializers.ValidationError(f'Недостаточно "{item.product.name}" на складе.')
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context['request']

        cart = Cart.objects.select_for_update().filter(user=request.user).order_by('pk').first()
        if not cart:
            raise serializers.ValidationError('Корзина пуста.')

        cart_items = list(cart.items.select_related('product'))
        if not cart_items:
            raise serializers.ValidationError('Корзина пуста.')

        # Блокируем товары в стабильном порядке, чтобы параллельные checkout
        # не могли продать один и тот же остаток дважды и реже ловили deadlock.
        product_ids = sorted({item.product_id for item in cart_items})
        locked_products = {
            product.pk: product
            for product in Tobacco.objects.select_for_update()
            .filter(pk__in=product_ids)
            .order_by('pk')
        }

        # Теоретически товар мог быть удалён между чтением корзины и локом.
        missing_ids = set(product_ids) - set(locked_products)
        if missing_ids:
            raise serializers.ValidationError('Один из товаров больше недоступен.')

        order = Order.objects.create(user=request.user, total_price=0, **validated_data)
        order_items = []
        products_to_update = []

        for cart_item in cart_items:
            product = locked_products[cart_item.product_id]

            if product.requires_age_verification and not request.user.is_of_legal_age:
                raise serializers.ValidationError(
                    'Для покупки этого товара нужно подтвердить дату рождения '
                    'и возраст 18+ в профиле аккаунта.'
                )
            if not product.is_available:
                raise serializers.ValidationError(f'Товар "{product.name}" больше не доступен для заказа.')
            if cart_item.quantity < 1:
                raise serializers.ValidationError(f'Некорректное количество для "{product.name}".')
            if cart_item.quantity > product.stock_quantity:
                raise serializers.ValidationError(f'Недостаточно "{product.name}" на складе.')

            order_items.append(OrderItem(
                order=order,
                tobacco=product,
                product_name=product.name,
                quantity=cart_item.quantity,
                price=product.price,
            ))
            product.stock_quantity -= cart_item.quantity
            products_to_update.append(product)

        # Было: по одному INSERT OrderItem + UPDATE Tobacco на каждую позицию.
        # Теперь независимо от размера корзины это два bulk-запроса.
        OrderItem.objects.bulk_create(order_items)
        Tobacco.objects.bulk_update(products_to_update, ['stock_quantity'])

        # Все цены уже известны в памяти — не нужен дополнительный SELECT по
        # order.items только ради суммы.
        order.total_price = sum((item.price * item.quantity for item in order_items), start=0)
        order.save(update_fields=['total_price'])

        cart.items.all().delete()
        return order
