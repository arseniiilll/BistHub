from rest_framework import serializers
from django.db import transaction
from .models import Order, OrderItem
from apps.cart.models import Cart
from apps.main.models import Tobacco


class OrderItemSerializer(serializers.ModelSerializer):
    # product_name — снимок имени на момент покупки. Показываем его, а если он
    # почему-то пуст (например, запись создана через bulk_create(), который
    # обходит OrderItem.save() и не проставляет product_name) — подстраховываемся
    # живым tobacco.name, чтобы не отдавать в API пустое имя товара.
    tobacco_name = serializers.SerializerMethodField()
    total_price = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ['id', 'tobacco', 'tobacco_name', 'quantity', 'price', 'total_price']
        # Позиции заказа фиксируют факт покупки и не должны редактироваться
        # постфактум (см. комментарий в admin.py) — поэтому tobacco и quantity
        # тоже read-only, а не полагаемся на то, что сериализатор сейчас
        # используется только с read_only=True снаружи.
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
        # Быстрый предварительный чек без блокировки — чтобы сразу отбить
        # пустую корзину / несовершеннолетнего пользователя без похода в create().
        # Финальная, атомарная проверка остатков — внутри create(), под select_for_update.
        request = self.context['request']
        user = request.user

        cart = Cart.objects.filter(user=user).first()
        if not cart or not cart.items.exists():
            raise serializers.ValidationError('Корзина пуста.')

        cart_items = list(cart.items.select_related('product'))

        requires_age_check = any(item.product.requires_age_verification for item in cart_items)
        if requires_age_check and not user.is_of_legal_age:
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

        # Блокируем саму корзину, чтобы два параллельных "оформить заказ"
        # по одной и той же корзине не создали два заказа из одних позиций.
        # Используем filter().first() вместо .get(): если у пользователя почему-то
        # окажется больше одной корзины, .get() бросил бы необработанный
        # MultipleObjectsReturned (500) вместо аккуратной бизнес-ошибки.
        cart = Cart.objects.select_for_update().filter(user=request.user).order_by('pk').first()
        if not cart:
            # Между мягкой проверкой в validate() и захватом лока здесь
            # корзина могла исчезнуть (гонка/повторный сабмит) — это та же
            # бизнес-ошибка "корзина пуста", а не повод отдавать 500.
            raise serializers.ValidationError('Корзина пуста.')
        cart_items = list(cart.items.select_related('product'))

        if not cart_items:
            raise serializers.ValidationError('Корзина пуста.')

        # Блокируем товары в фиксированном порядке (по pk) — важно, чтобы
        # избежать deadlock, если два заказа одновременно берут разные
        # наборы одних и тех же товаров в разном порядке.
        product_ids = sorted({item.product_id for item in cart_items})
        locked_products = {
            p.pk: p
            for p in Tobacco.objects.select_for_update().filter(pk__in=product_ids).order_by('pk')
        }

        order = Order.objects.create(user=request.user, total_price=0, **validated_data)

        for cart_item in cart_items:
            product = locked_products[cart_item.product_id]

            # Повторяем проверки возраста и доступности здесь, на свежих,
            # заблокированных под select_for_update() объектах — а не
            # полагаемся на то, что было в корзине на момент validate().
            # Между validate() и захватом лока в create() параллельный
            # запрос мог добавить в корзину табачный товар (возраст ещё
            # не перепроверен) или товар успели снять с продажи.
            if product.requires_age_verification and not request.user.is_of_legal_age:
                raise serializers.ValidationError(
                    'Для покупки этого товара нужно подтвердить дату рождения '
                    'и возраст 18+ в профиле аккаунта.'
                )
            if not product.is_available:
                raise serializers.ValidationError(f'Товар "{product.name}" больше не доступен для заказа.')
            if cart_item.quantity > product.stock_quantity:
                raise serializers.ValidationError(f'Недостаточно "{product.name}" на складе.')
            # OrderItem.quantity объявлен с MinValueValidator(1), но .create()/.save()
            # не вызывают full_clean(), так что валидатор модели сам по себе не
            # защищает от quantity <= 0 — проверяем явно здесь, на реальном пути записи.
            if cart_item.quantity < 1:
                raise serializers.ValidationError(f'Некорректное количество для "{product.name}".')

            OrderItem.objects.create(
                order=order,
                tobacco=product,
                quantity=cart_item.quantity,
                price=product.price,
            )
            product.stock_quantity -= cart_item.quantity
            product.save(update_fields=['stock_quantity'])

        order.recalculate_total()
        cart.items.all().delete()
        return order