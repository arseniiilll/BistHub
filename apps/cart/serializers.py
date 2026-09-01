from rest_framework import serializers
from .models import Cart, CartItem


class CartItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_price = serializers.DecimalField(source='product.price', max_digits=8, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    # PositiveIntegerField в Django, несмотря на название, пропускает 0 —
    # min_value=1 закрывает эту дыру на уровне валидации API.
    quantity = serializers.IntegerField(min_value=1)

    class Meta:
        model = CartItem
        fields = ['id', 'product', 'product_name', 'product_price', 'quantity', 'total_price', 'added_at']
        read_only_fields = ['id', 'added_at']

    def validate_product(self, value):
        if not value.is_available:
            raise serializers.ValidationError('Товар недоступен для заказа.')
        return value

    def validate(self, attrs):
        # product можно задать только при создании позиции. При PATCH его
        # менять нельзя: иначе (а) проверка остатка в perform_update идёт
        # по старому instance.product_id мимо нового товара, и (б) смена на
        # товар, уже лежащий в корзине, падает в IntegrityError из-за
        # unique_together('cart', 'product') вместо аккуратного 400.
        # Если нужно сменить товар — удалить позицию и добавить новую.
        if self.instance is not None and 'product' in attrs and attrs['product'] != self.instance.product:
            raise serializers.ValidationError({
                'product': 'Товар в позиции корзины нельзя менять — удалите позицию и добавьте новую.'
            })

        product = attrs.get('product') or getattr(self.instance, 'product', None)
        quantity = attrs.get('quantity', getattr(self.instance, 'quantity', 1))
        if product and quantity > product.stock_quantity:
            raise serializers.ValidationError(f'На складе доступно только {product.stock_quantity} шт.')
        return attrs


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_items = serializers.IntegerField(read_only=True)

    class Meta:
        model = Cart
        fields = ['id', 'items', 'total_price', 'total_items', 'created_at', 'updated_at']