from django.contrib import admin
from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    """Состав заказа — только просмотр. Позиции заказа (цена, количество)
    фиксируют факт покупки и не должны редактироваться постфактум:
    любая коррекция — это новый заказ/возврат, а не правка старого."""
    model = OrderItem
    extra = 0
    readonly_fields = ['tobacco', 'quantity', 'price', 'get_total_price']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Total')
    def get_total_price(self, obj):
        return obj.get_total_price()


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Единственный канал управления заказами: смена статуса (pending → processing →
    shipped → delivered/canceled) и просмотр состава — раз update/delete через API
    намеренно не предоставлены (см. комментарий в OrderViewSet)."""
    list_display = ['id', 'email', 'full_name', 'status', 'total_price', 'created']
    list_filter = ['status', 'created']
    search_fields = ['id', 'email', 'first_name', 'last_name', 'user__email']
    readonly_fields = [
        'user', 'first_name', 'last_name', 'email',
        'address1', 'address2', 'city', 'country', 'province', 'postal_code', 'phone',
        'total_price', 'created', 'updated',
    ]
    # status — единственное редактируемое поле: смена статуса и есть
    # основной рабочий сценарий менеджера заказов в админке.
    list_editable = ['status']
    inlines = [OrderItemInline]
    ordering = ['-created']

    @admin.display(description='Customer')
    def full_name(self, obj):
        return f'{obj.first_name} {obj.last_name}'.strip()

    def has_add_permission(self, request):
        # Заказы создаются только через чекаут (OrderCreateSerializer),
        # не вручную из админки — там нет логики списания стока/лока корзины.
        return False

    def has_delete_permission(self, request, obj=None):
        # Заказы — источник истины для истории покупок и бухгалтерии.
        # Отмена оформляется сменой статуса на CANCELED, а не удалением записи.
        return False