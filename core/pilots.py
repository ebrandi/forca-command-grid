"""Active pilot context — which of an account's linked pilots the user is flying (LP-2).

A FORCA account (``identity.User``) is a *human*; an ``sso.EveCharacter`` is a *pilot*. A human
may have several pilots linked, each authorised individually through EVE SSO, each with its own
tokens, corporation, roles, skills, assets and history. Nothing about a pilot is ever merged
with another (LP-3) — selecting a pilot changes *which* pilot the app acts as, and that is all.

The selection is a **hint held server-side in the session** (Django's session backend is the
database, so the browser holds only an opaque key and cannot forge a pilot id). That is still
not treated as authorisation: :func:`resolve_active_pilot` re-checks on **every request** that
the pilot is currently linked to the signed-in user, so a pilot unlinked in another tab — or
detached by an officer — collapses back to a safe default on the very next request rather than
lingering for the life of the session.

The resolved pilot is attached to the ``User`` instance rather than threaded through ~100 call
signatures. That is how this codebase already carries request-scoped state (see
``User.max_role_rank``'s memo), and it is what lets ``core.rbac`` — whose functions take a
``user``, not a ``request`` — become pilot-aware at a single seam.
"""
from __future__ import annotations

import contextvars

# The session key holding the pilot the user last selected. A *hint*: it is re-validated
# against the database on every request and discarded if it no longer resolves to a pilot
# linked to this account.
SESSION_KEY = "active_pilot_id"

# The request's resolved pilot, as ``(user_pk, character)``. A backstop for the attribute below.
#
# The attribute lives on ONE ``User`` instance. A view that re-fetches its own user —
# ``User.objects.get(pk=request.user.pk)``, a ``refresh_from_db()``, a queryset that happens to
# return the same row — gets a *different* instance, with no pilot attached, and would then be
# judged by ``core.rbac`` as if no request had resolved one: account-wide authority, ceiling
# bypassed. That failure is silent and invisible, and it is one ``refresh_from_db()`` away.
#
# Matching on ``user_pk`` is what keeps this honest. A DIFFERENT user object (an officer's
# console ranking every member) must NOT inherit the requester's pilot — for them the
# account-wide question is the right one, because we are not them.
#
# contextvars are per-thread in sync WSGI, and the middleware resets the token in a finally, so
# nothing leaks into the next request served by the same worker thread.
_REQUEST_PILOT: contextvars.ContextVar = contextvars.ContextVar("forca_request_pilot", default=None)

# Set on the User instance by ActivePilotMiddleware. ``_ACTIVE_RESOLVED`` is the honest half:
# its ABSENCE means "no request resolved a pilot here" (a Celery worker, a management command),
# which is a legitimately different question from "this user has no pilots" — and the two must
# not be confused, because rbac falls back to account-wide authority in the former case.
_ACTIVE_ATTR = "_active_pilot"
_ACTIVE_RESOLVED = "_active_pilot_resolved"


_ROSTER_MEMO = "_linked_pilots_cache"


def linked_pilots(user, *, with_tokens: bool = False):
    """Every pilot linked to ``user``, ordered for the selector.

    Order: the user's explicit ``display_order`` first (all zero until they reorder), then the
    main pilot, then most-recently-used, then alphabetically. The *active* pilot is hoisted
    to the top by :func:`ordered_for_selector`, which is a presentation concern, not this one.

    Memoised on the ``User`` instance, because this is now on the hot path of EVERY
    authenticated request twice over — the middleware resolves the active pilot from it, and
    the ``roles`` context processor renders the selector from it. Without the memo that is two
    round trips per page where there used to be one (``user.main_character``), and the query
    budgets in tests/test_*_perf.py catch it. Request-scoped in effect: a fresh ``User`` is
    loaded per request. Writers that change the roster call :func:`invalidate`.

    ``with_tokens`` prefetches each pilot's tokens, for the management page's detailed health
    panel. It is never memoised, and the sidebar selector does NOT ask for it — that renders on
    every authenticated page and gets its warning dot from one aggregate query instead
    (``linking.healthy_ids``).
    """
    if not getattr(user, "is_authenticated", False):
        return []
    if with_tokens:
        qs = user.characters.select_related(
            "corporation", "corporation__alliance"
        ).prefetch_related("tokens")
        return sorted(qs.all(), key=_selector_key)

    cached = user.__dict__.get(_ROSTER_MEMO)
    if cached is None:
        qs = user.characters.select_related("corporation", "corporation__alliance")
        cached = sorted(qs.all(), key=_selector_key)
        user.__dict__[_ROSTER_MEMO] = cached
    return cached


def invalidate(user) -> None:
    """Drop the memoised roster after a link, unlink, reorder or main-pilot change."""
    user.__dict__.pop(_ROSTER_MEMO, None)


def _selector_key(character):
    # -timestamp so that "more recent" sorts earlier; never-used pilots sort last among peers.
    last_used = character.last_used_at.timestamp() if character.last_used_at else 0.0
    return (
        character.display_order,
        0 if character.is_main else 1,
        -last_used,
        (character.name or "").casefold(),
    )


def ordered_for_selector(user, *, with_tokens: bool = False):
    """:func:`linked_pilots` with the active pilot hoisted to the top.

    Python's sort is stable, so hoisting the active pilot leaves every other pilot in the order
    :func:`linked_pilots` chose.
    """
    pilots = linked_pilots(user, with_tokens=with_tokens)
    current = active_pilot(user)
    if current is None:
        return pilots
    return sorted(pilots, key=lambda c: c.character_id != current.character_id)


def active_pilot(user):
    """The pilot ``user`` is currently flying, or ``None``.

    Reads the attribute the middleware set; falls back to the request-scoped context var when
    this is a *different instance of the same user* (see ``_REQUEST_PILOT``). Returns ``None``
    outside a request — callers that need a pilot in a background job must pass it explicitly
    rather than reaching for a session that does not exist.
    """
    if getattr(user, _ACTIVE_RESOLVED, False):
        return getattr(user, _ACTIVE_ATTR, None)
    return _from_request_context(user)


def _from_request_context(user):
    context = _REQUEST_PILOT.get()
    if context is None:
        return None
    user_pk, character = context
    return character if user_pk == getattr(user, "pk", None) else None


def acting_pilot(user):
    """The pilot the app should act AS for this user, in any context. Use this everywhere.

    This is the canonical replacement for the ~80 call sites that used to spell "me" as
    ``user.main_character`` or, inlined by hand, ``next((c for c in chars if c.is_main), chars[0])``.
    Every one of them meant "the pilot this user is currently being", and every one of them was
    wrong the moment a user could be more than one pilot.

    * In a request: the ACTIVE pilot, as resolved and ownership-checked by ActivePilotMiddleware.
    * Outside a request (Celery, a management command): the account's main pilot — the same
      answer as before, because a background job has no session to have selected anything, and
      "the main" is the account's own default.

    Deliberately NOT a change to ``User.main_character``. That property is dual-purpose: it also
    resolves *other people's* display names in leaderboards, recognition feeds and the members
    console, where "the pilot THAT user is currently flying" is neither knowable nor wanted.
    """
    if has_resolved_pilot(user):
        return active_pilot(user)
    return getattr(user, "main_character", None)


def has_resolved_pilot(user) -> bool:
    """True when THIS request resolved an active pilot for THIS user.

    False in a Celery worker or a management command, where authority is account-wide by
    design (a nightly Discord role sync reconciles what *the human* is entitled to, not what
    some browser session happens to have selected).

    Also true for a re-fetched instance of the request's own user, via ``_REQUEST_PILOT`` — the
    ceiling must not be escapable by loading yourself out of the database again.
    """
    if getattr(user, _ACTIVE_RESOLVED, False):
        return True
    context = _REQUEST_PILOT.get()
    return context is not None and context[0] == getattr(user, "pk", None)


def attach(user, character) -> None:
    """Record the resolved pilot on the user instance (called by the middleware only)."""
    setattr(user, _ACTIVE_ATTR, character)
    setattr(user, _ACTIVE_RESOLVED, True)
    # The rank memo may have been computed before the pilot was known; drop it so the
    # pilot-scoped ceiling is applied (see core.rbac.effective_rank).
    user.__dict__.pop("_max_role_rank_cache", None)
    user.__dict__.pop("_perm_keys_cache", None)


def owned_pilot(user, character_id):
    """The user's pilot with this id, or ``None``.

    The ownership check *is* the query: it filters on the server-derived ``user``, so a
    character id the caller does not own simply does not resolve. There is no code path in
    which an id from the client selects a row that is then checked afterwards — which is the
    shape IDOR bugs take.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    try:
        cid = int(character_id)
    except (TypeError, ValueError):
        return None
    return (
        user.characters.select_related("corporation", "corporation__alliance")
        .filter(character_id=cid)
        .first()
    )


def resolve_active_pilot(request):
    """Resolve the active pilot for ``request.user``, validating the session hint.

    Never trusts the session value: it is only used if it still names a pilot linked to this
    account. Falls back to the main pilot, then the first in selector order, then ``None``
    (an account whose pilots were all detached — possible, and it must not crash).
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return None

    # A director "view-as" must not read the DIRECTOR's pilot choice, and — being view-only —
    # must not write the target's either. The impersonated pilot is shown their own main.
    if getattr(request, "is_impersonating", False):
        roster = linked_pilots(user)
        return roster[0] if roster else None

    # One query for the whole roster (memoised), and the hint is matched against it — rather
    # than a second query for the hinted pilot and a third for the fallback. The ownership
    # check is unchanged: the roster IS the set of pilots this user holds, so a hint naming
    # anything else simply does not match.
    roster = linked_pilots(user)
    hinted = request.session.get(SESSION_KEY)
    if hinted is not None:
        for pilot in roster:
            if pilot.character_id == hinted:
                return pilot
        # The hint is stale (unlinked, or detached by an officer). Drop it rather than let it
        # resolve again next request.
        request.session.pop(SESSION_KEY, None)

    return roster[0] if roster else None


def select(request, character) -> None:
    """Make ``character`` the active pilot for this session and stamp its recency.

    The caller is responsible for having resolved ``character`` through :func:`owned_pilot`.
    """
    from django.utils import timezone

    request.session[SESSION_KEY] = character.character_id
    character.last_used_at = timezone.now()
    character.save(update_fields=["last_used_at"])
    # last_used_at is part of the selector's sort key, so the memoised roster is now stale.
    invalidate(request.user)
    attach(request.user, character)


class ActivePilotMiddleware:
    """Resolve the active pilot once per request, before anything asks about authority.

    Sits immediately after ``ImpersonationMiddleware`` (which may have swapped ``request.user``,
    and whose swap we must honour) and before ``MembershipGateMiddleware`` /
    ``FeatureGateMiddleware`` / the ``roles`` context processor — all of which reduce to
    ``core.rbac.effective_rank``, which now asks the active pilot what it may substantiate.

    It runs unconditionally for authenticated users, including when the account has no pilots
    at all: :func:`attach` is still called (with ``None``), so ``has_resolved_pilot`` is true
    and the rank ceiling fails **closed** at ``public``. The absence of that marker means
    "nothing resolved a pilot", which only happens outside a request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.active_pilot = None
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return self.get_response(request)

        pilot = resolve_active_pilot(request)
        attach(user, pilot)
        request.active_pilot = pilot
        # Also publish it request-wide, so a re-fetched instance of this same user cannot slip
        # past the authority ceiling (see _REQUEST_PILOT). Reset in a finally: WSGI reuses
        # worker threads, and a leaked pilot would be the next request's active pilot.
        token = _REQUEST_PILOT.set((user.pk, pilot))
        try:
            return self.get_response(request)
        finally:
            _REQUEST_PILOT.reset(token)
