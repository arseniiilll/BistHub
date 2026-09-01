from django.core.validators import MinValueValidator
from django.db import models
from django.conf import settings
from decimal import Decimal

from apps.main.models import Tobacco


class Order(models.Model):
    class StatusChoices(models.TextChoices):
        PENDING = 'pending', "Pending"
        PROCESSING = 'processing', "Processing"
        SHIPPED = 'shipped', "Shipped"
        DELIVERED = 'delivered', "Delivered"
        CANCELED = 'canceled', "Canceled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='orders')

    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    email = models.EmailField(max_length=254)
    address1 = models.CharField(max_length=50, null=True, blank=True)
    address2 = models.CharField(max_length=50, null=True, blank=True)
    city = models.CharField(max_length=50, null=True, blank=True)
    country = models.CharField(max_length=50, null=True, blank=True)
    province = models.CharField(max_length=50, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    phone = models.CharField(max_length=15, null=True, blank=True)

    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.PENDING)
    hidden_from_history = models.BooleanField(default=False)

    # Платёжные детали (provider, stripe id, статус транзакции) живут только в Payment.
    # Order не должен знать о конкретном payment-провайдере.

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'orders'
        verbose_name = 'Order'
        verbose_name_plural = 'Orders'
        ordering = ['-created']

    def __str__(self):
        return f"Order {self.id} by {self.email}"

    def recalculate_total(self, save=True):
        """Пересчитывает total_price на основе связанных OrderItem.
        Вызывать после любого добавления/изменения/удаления позиций заказа."""
        total = sum(
            (item.get_total_price() for item in self.items.all()),
            Decimal('0.00')
        )
        self.total_price = total
        if save:
            self.save(update_fields=['total_price'])
        return total

    @property
    def latest_payment(self):
        return self.payments.order_by('-created_at').first()


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    # PROTECT, а не CASCADE: товар нельзя удалить, если по нему есть заказы —
    # иначе удаление товара из каталога молча стёрло бы позиции из чужой
    # истории покупок и рассинхронизировало бы total_price заказа.
    tobacco = models.ForeignKey(
        Tobacco, on_delete=models.PROTECT, related_name='order_items',
    )
    # Денормализованное имя товара на момент покупки. Позволяет корректно
    # показывать состав старого заказа, даже если сам товар потом переименуют.
    product_name = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    price = models.DecimalField(max_digits=10, decimal_places=2)  # цена на момент покупки

    class Meta:
        verbose_name = 'Order item'
        verbose_name_plural = 'Order items'

    def __str__(self):
        return f"{self.product_name or self.tobacco.name} - {self.quantity}"

    def save(self, *args, **kwargs):
        # Фиксируем имя товара один раз, при создании позиции.
        if not self.product_name and self.tobacco_id:
            self.product_name = self.tobacco.name
        super().save(*args, **kwargs)

    def get_total_price(self):
        return self.price * self.quantity