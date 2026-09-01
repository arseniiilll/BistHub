# Generated migration for adding metadata field to Refund

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0006_update_payment_statuses'),  # Исправлено: используем правильную последнюю миграцию
    ]

    operations = [
        migrations.AddField(
            model_name='refund',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
