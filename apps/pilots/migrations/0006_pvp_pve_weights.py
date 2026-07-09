from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pilots", "0005_readd_train_add_doctrine_kinds"),
    ]

    operations = [
        migrations.AddField(
            model_name="contributionweights",
            name="pvp_points_per_kill",
            field=models.IntegerField(default=1, help_text="Points per enemy kill the pilot was involved in."),
        ),
        migrations.AddField(
            model_name="contributionweights",
            name="pvp_final_blow_bonus",
            field=models.IntegerField(default=0, help_text="Extra points when the pilot landed the final blow."),
        ),
        migrations.AddField(
            model_name="contributionweights",
            name="pve_points_per_mil",
            field=models.DecimalField(decimal_places=3, default=Decimal("0.050"), help_text="Points per 1,000,000 ISK of corp PVE (ratting) income from a member.", max_digits=8),
        ),
        migrations.AddField(
            model_name="contributionweights",
            name="pve_ref_types",
            field=models.CharField(default="bounty_prizes,ess_escrow_transfer", help_text="Corp wallet ref_types that count as members' PVE income (comma-separated). The Corp Finance page shows your real ref_types.", max_length=255),
        ),
    ]
