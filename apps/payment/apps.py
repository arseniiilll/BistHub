from django.apps import AppConfig


class PaymentConfig(AppConfig):
    # Явно фиксируем тип автополя на уровне приложения — не зависим от
    # DEFAULT_AUTO_FIELD в settings.py, чтобы поведение не менялось при
    # правках глобального конфига.
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.payment'