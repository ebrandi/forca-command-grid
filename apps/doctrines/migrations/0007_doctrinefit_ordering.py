from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("doctrines", "0006_doctrinedisplayconfig"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="doctrinefit",
            options={"ordering": ["id"]},
        ),
    ]
