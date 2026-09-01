from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from apps.main.models import Tobacco
from decimal import Decimal



class Cart(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='cart')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def total_price(self):
        return sum((item.total_price for item in self.items.all()), Decimal('0'))

    @property
    def total_items(self):
        return sum(item.quantity for item in self.items.all())

    def __str__(self):
        return f"Cart of: {self.user}"




class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    # related_name='cart_items' — иначе обратный доступ с Tobacco был бы
    # безликим tobacco.cartitem_set, не в стиле остального проекта
    # (сравни Review.tobacco -> related_name='reviews').
    product = models.ForeignKey(Tobacco, on_delete=models.CASCADE, related_name='cart_items')
    # PositiveIntegerField пропускает 0 (несмотря на название — запрещает
    # только отрицательные). min_value=1 в CartItemSerializer защищает
    # только запросы через API; MinValueValidator(1) здесь закрывает дыру
    # для любого другого пути записи (админка, сигналы, дата-миграции,
    # фикстуры), где иначе можно было бы завести "позицию из 0 штук".
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('cart', 'product')

    @property
    def total_price(self):
        return self.product.price * self.quantity

    def __str__(self):
        return f"{self.quantity} x {self.product.name}"