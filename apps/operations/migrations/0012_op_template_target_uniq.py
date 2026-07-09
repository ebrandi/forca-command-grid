import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('operations', '0011_operationtemplate_operation_recurring_template_and_more'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='operation',
            constraint=models.UniqueConstraint(
                condition=models.Q(('recurring_template__isnull', False)),
                fields=('recurring_template', 'target_at'),
                name='op_template_target_uniq',
            ),
        ),
    ]
