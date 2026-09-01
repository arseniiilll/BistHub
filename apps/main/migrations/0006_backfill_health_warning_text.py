# Generated manually — 0005 (сгенерированная makemigrations) сменила поле
# на обязательное, но НЕ заполнила задним числом уже существующие пустые
# значения. Эта миграция закрывает именно это.

from django.db import migrations

DEFAULT_HEALTH_WARNING = (
    "Acest produs conține nicotină, care creează dependență. "
    "Fumatul dăunează grav sănătății dumneavoastră și celor din jur."
)


def backfill_health_warning(apps, schema_editor):
    Tobacco = apps.get_model('main', 'Tobacco')
    Tobacco.objects.filter(health_warning_text='').update(health_warning_text=DEFAULT_HEALTH_WARNING)


def noop_reverse(apps, schema_editor):
    # Не стираем текст обратно в '' — это была бы порча данных, а не откат схемы.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0005_alter_tobacco_health_warning_text'),
    ]

    operations = [
        migrations.RunPython(backfill_health_warning, noop_reverse),
    ]