from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0004_backfill_orderitem_product_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='hidden_from_history',
            field=models.BooleanField(default=False),
        ),
    ]
