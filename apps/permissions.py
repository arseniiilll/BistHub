from rest_framework import permissions


class IsOwner(permissions.BasePermission):
    """Разрешает изменение/удаление объекта только его владельцу.
    По умолчанию ищет поле `author`, для других моделей переопредели
    `owner_field` в самом ViewSet (например owner_field = 'user')."""

    default_owner_field = 'author'

    def has_object_permission(self, request, view, obj):
        owner_field = getattr(view, 'owner_field', self.default_owner_field)
        owner = getattr(obj, owner_field, None)
        return owner == request.user




class IsOfLegalAge(permissions.BasePermission):
    """Требует подтверждённый возраст 18+ (EU TPD / Law 201/2016).

    Намеренно закрывает доступ и тем, у кого date_of_birth вообще не указан
    (пользователи, зарегистрированные до введения поля) — см. User.is_of_legal_age:
    отсутствие даты рождения не считается подтверждением совершеннолетия.
    """
    message = 'Для работы с корзиной необходимо подтвердить возраст 18+ в профиле.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_of_legal_age)