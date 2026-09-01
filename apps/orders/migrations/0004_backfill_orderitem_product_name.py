# Бэкфилл product_name для позиций заказов, созданных до миграции 0003,
# когда поля product_name ещё не существовало. Без этого шага у всех
# заказов, оформленных до апдейта, tobacco_name в API станет пустым —
# сериализатор теперь берёт имя только из product_name, а не из tobacco.name.

from django.db import migrations


def backfill(apps, schema_editor):
    OrderItem = apps.get_model('orders', 'OrderItem')
    qs = OrderItem.objects.filter(product_name='').select_related('tobacco')
    for item in qs.iterator():
        # tobacco всегда есть на момент этой миграции: поле обязательное
        # (nullable=False), PROTECT ещё не успел бы помешать существующим ссылкам.
        if item.tobacco_id:
            item.product_name = item.tobacco.name
            OrderItem.objects.filter(pk=item.pk).update(product_name=item.product_name)


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0003_orderitem_product_name_alter_orderitem_quantity_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]