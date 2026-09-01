# Generated migration for payment statuses update

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0005_alter_payment_stripe_payment_intent_id_and_more'),  # Измените на последнюю вашу миграцию
    ]

    operations = [
        migrations.AlterField(
            model_name='payment',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('processing', 'Processing'),
                    ('succeeded', 'Succeeded'),
                    ('partially_refunded', 'Partially Refunded'),
                    ('refunded', 'Fully Refunded'),
                    ('failed', 'Failed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='pending',
                max_length=20
            ),
        ),
    ]
