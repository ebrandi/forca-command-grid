"""Shared builders for the raffle test suite (not collected — no ``test_`` prefix).

Encapsulates the exact fixture idioms the raffle subsystem needs: an enrolled
pilot with a VALID (encrypted) ESI token, a home-corp killmail crediting a pilot
as attacker, and a ready-to-use contest with seeded sources.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail, KillmailParticipant
from apps.raffle import services
from apps.raffle.models import RaffleContest, RafflePrize
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001  # == settings.FORCA_HOME_CORP_ID in config.settings.test


def make_user(django_user_model, username, *roles):
    """A bare user with the given role assignments."""
    user = django_user_model.objects.create(username=username)
    for role in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


def add_token(character, *, scopes=None, hours=1):
    """Attach a VALID token. The property SETTERS encrypt — required or
    ``is_valid`` / eligibility stays False (the raw ``_refresh_token`` column
    would be empty)."""
    token = AuthToken(
        character=character,
        scopes=list(scopes) if scopes is not None else ["publicData"],
        access_expires_at=timezone.now() + timedelta(hours=hours),
    )
    token.refresh_token = "r"
    token.access_token = "a"
    token.save()
    return token


def enrol_pilot(django_user_model, character_id, *, username=None, name=None,
                roles=(rbac.ROLE_MEMBER,), is_corp_member=True, with_token=True,
                scopes=None, enrolled_days_ago=60):
    """A fully enrolled pilot: user + main EveCharacter + (optional) valid token.

    ``enrolled_days_ago`` sets the character's ``added_at`` (enrolment time). It
    defaults to 60 days ago so the pilot counts as enrolled BEFORE a test contest —
    the non-retroactive gate then awards their in-contest activity. Pass a small /
    negative value to model a pilot who enrolled after (or during) the activity.

    Returns ``(user, character)``.
    """
    username = username or f"pilot-{character_id}"
    name = name or f"Pilot {character_id}"
    user = make_user(django_user_model, username, *roles)
    character = EveCharacter.objects.create(
        character_id=character_id, user=user, name=name,
        is_main=True, is_corp_member=is_corp_member,
        added_at=timezone.now() - timedelta(days=enrolled_days_ago),
    )
    user.main_character_id = character_id
    user.save()
    if with_token:
        add_token(character, scopes=scopes)
    return user, character


def detached_character(character_id, *, name=None, is_corp_member=True):
    """An EveCharacter with no account (never claimed / GDPR-erased) → not enrolled."""
    return EveCharacter.objects.create(
        character_id=character_id, user=None,
        name=name or f"Detached {character_id}", is_main=False,
        is_corp_member=is_corp_member,
    )


def make_contest(*, name="Test Raffle", status=RaffleContest.Status.ACTIVE,
                 start_days_ago=7, end_days_ahead=7, draw_days_ahead=8,
                 seed_sources=True, **extra):
    now = timezone.now()
    contest = RaffleContest.objects.create(
        name=name, status=status,
        start_at=now - timedelta(days=start_days_ago),
        end_at=now + timedelta(days=end_days_ahead),
        draw_at=now + timedelta(days=draw_days_ahead),
        **extra,
    )
    if seed_sources:
        services.seed_source_configs(contest)
    return contest


def enable_source_retroactive(contest, source_key="pvp"):
    cfg = contest.source_configs.filter(source_key=source_key).first()
    cfg.retroactive = True
    cfg.save(update_fields=["retroactive", "updated_at"])
    return cfg


def add_prizes(contest, n=2):
    """``n`` ISK prizes, rank 1 the richest."""
    prizes = []
    for rank in range(1, n + 1):
        prizes.append(RafflePrize.objects.create(
            contest=contest, rank=rank, name=f"Prize {rank}",
            estimated_value=Decimal("1000000") * (n - rank + 1),
        ))
    return prizes


def home_kill(km_id, *, attackers, is_solo=False, is_npc=False, is_awox=False,
              victim_char=666, victim_corp=999, victim_alliance=None,
              value="50000000", when=None, ship_type_id=587):
    """A home-corp ATTACKER killmail.

    ``attackers`` is a list of ``(character_id, corporation_id, final_blow)``.
    ``when`` defaults to 1h ago so it sits strictly inside a live accrual window.
    """
    when = when or (timezone.now() - timedelta(hours=1))
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=when,
        solar_system_id=30000142, victim_ship_type_id=ship_type_id,
        total_value=Decimal(value), is_solo=is_solo, is_npc=is_npc, is_awox=is_awox,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.ATTACKER,
        victim_character_id=victim_char, victim_corporation_id=victim_corp,
        victim_alliance_id=victim_alliance,
    )
    for seq, (cid, corp, final_blow) in enumerate(attackers):
        KillmailParticipant.objects.create(
            killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=seq,
            character_id=cid, corporation_id=corp, ship_type_id=ship_type_id,
            final_blow=final_blow, damage_done=100,
        )
    return km


def approved_total(contest):
    """Sum of APPROVED ledger amounts (the drawable total)."""
    from django.db.models import Sum

    from apps.raffle.models import RaffleTicketLedgerEntry

    return (
        RaffleTicketLedgerEntry.objects.filter(
            contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED
        ).aggregate(n=Sum("amount"))["n"] or 0
    )
