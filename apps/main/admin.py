from django.contrib import admin
from .models import Tobacco, Review


class ReviewInline(admin.TabularInline):
    """Поля отзыва только для чтения (модератор не должен править чужой текст/оценку),
    но can_delete=True — снять оскорбительный/спамный отзыв можно прямо со страницы товара,
    не уходя в отдельный ReviewAdmin."""
    model = Review
    extra = 0
    readonly_fields = ['author', 'rating', 'text', 'created_at']
    can_delete = True

    def get_queryset(self, request):
        # 'author' — readonly-поле, но отображается через str(author) —
        # без select_related это отдельный запрос на каждую строку отзыва.
        return super().get_queryset(request).select_related('author')

    def has_add_permission(self, request, obj=None):
        # Все поля readonly, так что пустая форма добавления всё равно
        # нерабочая — явно её отключаем, а не оставляем битой ссылкой.
        return False


@admin.register(Tobacco)
class TobaccoAdmin(admin.ModelAdmin):
    list_display = ['name', 'flavor', 'strength', 'price', 'stock_quantity', 'is_available', 'created_at']
    list_filter = ['strength', 'is_available']
    search_fields = ['name', 'flavor']
    # slug оставлен readonly и генерируется в Tobacco.save() / _generate_unique_slug().
    # Раньше здесь стоял prepopulated_fields = {'slug': ('name',)}: это чисто JS-подстановка
    # на клиенте, она проставляет slug ДО сабмита формы, поэтому save() видит непустой slug
    # и пропускает проверку уникальности с суффиксом — два товара с одинаковым name,
    # добавленные через админку, падали в IntegrityError. Readonly убирает эту лазейку.
    readonly_fields = ['slug', 'created_at']
    inlines = [ReviewInline]
    list_editable = ['price', 'stock_quantity', 'is_available']
    ordering = ['-created_at']


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['author', 'tobacco', 'rating', 'created_at']
    list_filter = ['rating']
    search_fields = ['author__email', 'tobacco__name']
    readonly_fields = ['author', 'tobacco', 'rating', 'text', 'created_at']
    ordering = ['-created_at']
    list_select_related = ['author', 'tobacco']

    def has_add_permission(self, request):
        # Все поля readonly, так что пустая форма добавления всё равно
        # нерабочая — явно её отключаем, а не оставляем битой кнопкой.
        return False