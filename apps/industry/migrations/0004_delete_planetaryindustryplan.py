"""Retire the duplicated in-industry Planetary code (roadmap 0.14 / IND-5).

``apps/industry.pi`` + ``PlanetaryIndustryPlan`` were superseded by ``apps/planetary``
(the live PI planner at /industry/pi/); nothing reads the model any more (its only
writers were the demo seed and a GDPR-erasure line, both removed). Prod held 2 stale
demo rows.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("industry", "0003_industryeconomyconfig_industryproject_archived_at_and_more")]
    operations = [migrations.DeleteModel(name="PlanetaryIndustryPlan")]
