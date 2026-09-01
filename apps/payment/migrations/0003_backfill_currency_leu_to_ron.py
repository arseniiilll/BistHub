from django.db import migrations


def fix_currency(apps, schema_editor):
    """
    Миграция 0002 поменяла default 'LEU' -> 'RON', но default применяется
    только к новым записям. Если приложение уже успело поработать между
    0001 и 0002 (staging/прод), в базе могли остаться Payment с
    currency='LEU' — это невалидный ISO 4217 код, который сломает любой
    повторный запрос к Stripe по такой записи (например, рефанд).
    Бэкфиллим их на 'RON'.
    """
    Payment = apps.get_model('payment', 'Payment')
    Payment.objects.filter(currency='LEU').update(currency='RON')


def reverse_fix_currency(apps, schema_editor):
    # Осознанно no-op: откатить нельзя понять, какие записи были 'LEU'
    # до прямой миграции, а откатывать RON -> LEU для всех записей
    # некорректно (часть могла быть создана уже как RON).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0002_alter_payment_currency_alter_payment_order'),
    ]

    operations = [
        migrations.RunPython(fix_currency, reverse_fix_currency),
    ]