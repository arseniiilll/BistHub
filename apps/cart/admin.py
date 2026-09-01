from django.contrib import admin
from .models import Cart, CartItem


class CartItemInline(admin.TabularInline):
    """Только просмотр. Редактирование product/quantity прямо из админки
    обошло бы проверку остатка и select_for_update() из CartItemViewSet —
    можно было бы тихо завести quantity выше stock_quantity или поймать
    IntegrityError на unique_together('cart', 'product') при смене товара
    на тот, что уже лежит в этой же корзине. Убрать позицию (can_delete)
    для поддержки безопасно — это не требует пересчёта остатка."""
    model = CartItem
    extra = 0
    readonly_fields = ['product', 'quantity', 'item_total_price', 'added_at']
    can_delete = True

    def get_queryset(self, request):
        # Все readonly-поля читают product (item_total_price -> total_price),
        # поэтому без select_related на каждую позицию корзины уходил
        # отдельный запрос за товаром.
        return super().get_queryset(request).select_related('product')

    def has_add_permission(self, request, obj=None):
        # Все поля readonly, так что пустая форма добавления всё равно
        # нерабочая — явно её отключаем, а не оставляем битой кнопкой.
        return False

    def item_total_price(self, obj):
        return obj.total_price
    item_total_price.short_description = 'Total price'


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ['user', 'total_items', 'total_price', 'created_at', 'updated_at']
    search_fields = ['user__email']
    readonly_fields = ['user', 'created_at', 'updated_at']
    inlines = [CartItemInline]
    ordering = ['-updated_at']

    def get_queryset(self, request):
        # total_items/total_price читают cart.items.all() на КАЖДУЮ строку
        # списка — без prefetch это N+1 запросов на страницу списка.
        # select_related('user') нужен отдельно: колонка 'user' в list_display
        # вызывает str(obj.user), и без него это ещё один запрос на строку.
        return super().get_queryset(request).select_related('user').prefetch_related('items__product')

    def has_add_permission(self, request):
        # У Cart всего 3 поля: created_at/updated_at всегда non-editable
        # (auto_now_add/auto_now), а 'user' здесь readonly — то есть форма
        # добавления всё равно без единого редактируемого поля. Корзина и так
        # создаётся автоматически через get_or_create в CartView/perform_create.
        return False

    def total_items(self, obj):
        return obj.total_items

    def total_price(self, obj):
        return obj.total_price


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ['cart', 'product', 'quantity', 'item_total_price', 'added_at']
    search_fields = ['cart__user__email', 'product__name']
    readonly_fields = ['cart', 'product', 'quantity', 'added_at']
    ordering = ['-added_at']
    list_select_related = ['cart__user', 'product']

    def has_add_permission(self, request):
        # Все поля readonly, так что пустая форма добавления всё равно
        # нерабочая — явно её отключаем, а не оставляем битой кнопкой.
        return False

    def item_total_price(self, obj):
        return obj.total_price
    item_total_price.short_description = 'Total price'