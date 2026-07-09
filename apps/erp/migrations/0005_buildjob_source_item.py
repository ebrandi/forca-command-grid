from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('erp', '0004_characterindustryjob'),
        ('industry', '0004_delete_planetaryindustryplan'),
    ]

    operations = [
        migrations.AddField(
            model_name='buildjob',
            name='source_item',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='build_jobs', to='industry.industryprojectitem',
            ),
        ),
    ]
