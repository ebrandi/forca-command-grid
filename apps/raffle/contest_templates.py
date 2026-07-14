"""Built-in contest templates — reusable blueprints leaders start a contest from.

A template's ``config`` has three parts: ``contest`` (field defaults),
``sources`` ({source_key: {enabled, mode, config, filters}}) and ``prizes`` (a
list of prize dicts). :func:`apply_template` writes them onto a draft contest;
:func:`seed_builtin_templates` upserts the built-ins (called from the data
migration and re-runnable safely).
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _


def _prizes(*values):
    names = ["1st prize", "2nd prize", "3rd prize", "4th prize", "5th prize"]
    return [
        {"rank": i + 1, "name": names[i], "prize_type": "isk",
         "estimated_value": v, "description": ""}
        for i, v in enumerate(values)
    ]


BUILTIN: list[dict] = [
    {
        "key": "pvp_activity", "name": _("PVP activity raffle"),
        "description": _("Reward everyone who undocks and gets on kills. Solo 100 · final blow 10 · participation 1."),
        "config": {
            "contest": {"objective": "Get pilots on more kills.",
                        "one_prize_per_pilot": True},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("2000000000", "1000000000", "500000000", "250000000", "100000000"),
        },
    },
    {
        "key": "solo_kill", "name": _("Solo kill challenge"),
        "description": _("Heavily reward solo PvP prowess — solo kills are worth 10× a normal kill."),
        "config": {
            "contest": {"objective": "Crown the corp's best solo hunter."},
            "sources": {"pvp": {"enabled": True, "mode": "auto",
                                 "config": {"per_kill": 1, "final_blow": 5, "solo": 250}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("1500000000", "750000000", "400000000", "200000000", "100000000"),
        },
    },
    {
        "key": "home_defence", "name": _("Home defence contest"),
        "description": _("Reward kills defending home space. Set the home region/systems in the PVP filters."),
        "config": {
            "contest": {"objective": "Defend home — kills in our space earn tickets."},
            "sources": {"pvp": {"enabled": True, "mode": "auto",
                                 "filters": {"exclude_blue": True}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("1000000000", "500000000", "250000000", "150000000", "75000000"),
        },
    },
    {
        "key": "mining_month", "name": _("Mining month"),
        "description": _("Reward ore mined from the corp mining ledger (X tickets per m³)."),
        "config": {
            "contest": {"objective": "Fill the ore hangar — mine to earn tickets."},
            "sources": {"mining": {"enabled": True, "mode": "auto",
                                    "config": {"basis": "m3", "per_ticket": 50000}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("800000000", "400000000", "200000000", "100000000", "50000000"),
        },
    },
    {
        "key": "industry_drive", "name": _("Industry production drive"),
        "description": _("Recognise builders. Industry has no reliable per-pilot feed, "
                         "so awards are officer-approved."),
        "config": {
            "contest": {"objective": "Keep the production lines running."},
            "sources": {"industry": {"enabled": True, "mode": "officer_approved"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("800000000", "400000000", "200000000", "100000000", "50000000"),
        },
    },
    {
        "key": "logistics_campaign", "name": _("Logistics support campaign"),
        "description": _("Reward haulers for delivered courier contracts (officer-approved)."),
        "config": {
            "contest": {"objective": "Keep the supply lines moving."},
            "sources": {"logistics": {"enabled": True, "mode": "officer_approved"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("500000000", "300000000", "150000000", "100000000", "50000000"),
        },
    },
    {
        "key": "newbro_support", "name": _("Newbro support contest"),
        "description": _("Reward mentors and helpers. Mentorship completions + leadership grants."),
        "config": {
            "contest": {"objective": "Help new pilots get flying."},
            "sources": {"mentorship": {"enabled": True, "mode": "officer_approved"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("500000000", "300000000", "150000000", "100000000", "50000000"),
        },
    },
    {
        "key": "alliance_deployment", "name": _("Alliance deployment raffle"),
        "description": _("PVP + fleet attendance during a deployment. Admits alliance / friendly pilots."),
        "config": {
            "contest": {"objective": "Show up and fight the deployment.",
                        "include_alliance": True},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "fleet": {"enabled": True, "mode": "auto", "config": {"per_op": 10}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("3000000000", "1500000000", "750000000", "400000000", "200000000"),
        },
    },
    {
        "key": "mixed_engagement", "name": _("Mixed activity engagement"),
        "description": _("A bit of everything — PVP, mining, fleet attendance and leadership grants."),
        "config": {
            "contest": {"objective": "Reward any way of contributing to the corp."},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "mining": {"enabled": True, "mode": "auto"},
                        "fleet": {"enabled": True, "mode": "auto"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("2000000000", "1000000000", "500000000", "250000000", "100000000"),
        },
    },
    {
        "key": "esi_adoption", "name": _("ESI adoption campaign"),
        "description": _("Drive app enrolment — everyone who connects ESI and flies earns; big CTA on the dashboard."),
        "config": {
            "contest": {"objective": "Get the whole corp enrolled in FORCA Command Grid.",
                        "show_ineligible_to_pilots": True},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("1000000000", "500000000", "250000000", "150000000", "100000000"),
        },
    },
]

BUILTIN_BY_KEY = {t["key"]: t for t in BUILTIN}


def apply_template(contest, template_key: str, *, overwrite_prizes: bool = False) -> bool:
    """Write a template's defaults onto a (draft) contest. Returns False if unknown."""
    from .models import RaffleContestTemplate, RafflePrize
    from .services import seed_source_configs

    tpl = RaffleContestTemplate.objects.filter(key=template_key, active=True).first()
    config = tpl.config if tpl else BUILTIN_BY_KEY.get(template_key, {}).get("config")
    if not config:
        return False

    # Contest field defaults (only known, safe fields).
    fields = config.get("contest", {})
    allowed = {
        "objective", "one_prize_per_pilot", "include_alliance",
        "show_ineligible_to_pilots", "retroactive_enabled", "public_rules",
    }
    dirty = []
    for k, v in fields.items():
        if k in allowed:
            setattr(contest, k, v)
            dirty.append(k)
    contest.template_key = template_key
    dirty.append("template_key")
    contest.save(update_fields=list(set(dirty)) + ["updated_at"])

    # Sources.
    seed_source_configs(contest)
    for key, sc in config.get("sources", {}).items():
        cfg = contest.source_configs.filter(source_key=key).first()
        if cfg is None:
            continue
        cfg.enabled = sc.get("enabled", cfg.enabled)
        cfg.mode = sc.get("mode", cfg.mode)
        if "config" in sc:
            cfg.config = {**cfg.config, **sc["config"]}
        if "filters" in sc:
            cfg.filters = {**cfg.filters, **sc["filters"]}
        cfg.save()

    # Prizes.
    if overwrite_prizes or not contest.prizes.exists():
        contest.prizes.all().delete()
        RafflePrize.objects.bulk_create([
            RafflePrize(contest=contest, rank=p["rank"], name=p["name"],
                        prize_type=p.get("prize_type", "isk"),
                        estimated_value=p.get("estimated_value", 0),
                        description=p.get("description", ""))
            for p in config.get("prizes", [])
        ])
    return True


def seed_builtin_templates() -> int:
    """Upsert the built-in templates. Idempotent — safe to re-run."""
    from .models import RaffleContestTemplate

    n = 0
    for t in BUILTIN:
        RaffleContestTemplate.objects.update_or_create(
            key=t["key"],
            defaults={"name": t["name"], "description": t["description"],
                      "config": t["config"], "built_in": True, "active": True},
        )
        n += 1
    return n
