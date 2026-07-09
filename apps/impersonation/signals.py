"""Close an open impersonation when the director logs out.

Django's ``logout()`` sends ``user_logged_out`` *before* it flushes the session, so the
impersonation keys are still readable here. Without this, "log out while viewing as a
pilot" would flush the cookie but leave the ImpersonationSession row open forever.
"""
from __future__ import annotations

from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver

from . import policy, services


@receiver(user_logged_out)
def _close_on_logout(sender, request, user, **kwargs):
    if request is None:
        return
    session = getattr(request, "session", None)
    if session is None or session.get(policy.SESSION_TARGET_KEY) is None:
        return
    # The real director is on request.impersonator during a swapped request; end() falls
    # back to the session's recorded actor id for the audit metadata regardless.
    services.end(request, reason="logout", actor=getattr(request, "impersonator", None))
