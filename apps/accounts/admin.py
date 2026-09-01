from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm as BaseUserCreationForm, UserChangeForm as BaseUserChangeForm

from .models import User


class UserCreationForm(BaseUserCreationForm):
    """Стоковый UserCreationForm жёстко прописывает Meta.fields = ('username',)
    и ничего не знает про email — при простом наследовании UserAdmin форма
    "добавить пользователя" в админке падает, потому что email там просто
    не окажется. Явно указываем модель и нужные поля."""
    class Meta(BaseUserCreationForm.Meta):
        model = User
        fields = ('email', 'username')


class UserChangeForm(BaseUserChangeForm):
    class Meta(BaseUserChangeForm.Meta):
        model = User
        fields = '__all__'


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = UserCreationForm
    form = UserChangeForm
    model = User

    ordering = ['-created_at']
    list_display = ['email', 'username', 'full_name', 'legal_age', 'is_staff', 'is_active', 'created_at']
    list_filter = ['is_staff', 'is_superuser', 'is_active']
    search_fields = ['email', 'username', 'first_name', 'last_name']
    readonly_fields = ['created_at', 'updated_at', 'last_login', 'date_joined']

    fieldsets = (
        (None, {'fields': ('email', 'username', 'password')}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'avatar', 'bio', 'date_of_birth')}),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        ('Important dates', {'fields': ('last_login', 'date_joined', 'created_at', 'updated_at')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'username', 'password1', 'password2'),
        }),
    )

    def legal_age(self, obj):
        return obj.is_of_legal_age
    legal_age.boolean = True
    legal_age.short_description = '18+'