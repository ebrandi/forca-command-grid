"""Conversational answer pipeline (P7, doc 17 §3) — worker-side (ADR-0008).

Answers one officer question grounded in the classification-filtered archive: retrieve the
passages the asker is cleared to see, ask the LLM to answer ONLY from them and cite passage
ids, keep only citations that resolve to real retrieved passages, and persist the turn for
audit. Read-only — it answers, it never acts. Degrades to a retrieval-only listing (still
useful, still classification-safe) when the LLM is disabled or unreachable, so the endpoint
never hard-fails on a provider outage.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from . import access, config, retrieval
from .models import ConversationTurn

logger = logging.getLogger("forca.command_intel")

_MAX_PASSAGES = 8


def _cite(p: dict) -> dict:
    return {"id": p["id"], "kind": p["kind"], "title": p["title"], "ref_url": p["ref_url"]}


def answer_question(turn: ConversationTurn) -> ConversationTurn:
    """Run retrieval + (if enabled) the grounded LLM answer for one turn. Never raises out."""
    try:
        turn.status = ConversationTurn.Status.ANSWERING
        turn.clearance = access.max_clearance(turn.user)
        turn.save(update_fields=["status", "clearance", "updated_at"])

        passages = retrieval.retrieve(turn.question, turn.user, k=_MAX_PASSAGES)
        valid_ids = {p["id"] for p in passages}

        if not settings.COMMAND_INTEL_ENABLED:
            _degraded(turn, passages, "AI is offline — here are the most relevant archive entries.")
            return turn

        from . import prompts
        from .llm.client import LLMError, LLMUnavailable

        try:
            from .llm.client import LLMClient

            client = LLMClient()
            req = prompts.build_chat_request(
                question=turn.question, passages=passages,
                provider_cfg=config.get("provider"), prompts_cfg=config.get("prompts"),
            )
            res = client.generate(req)
        except (LLMUnavailable, LLMError) as exc:
            _degraded(turn, passages, f"AI unavailable ({exc}). Here are the most relevant archive entries.")
            return turn

        obj = res.obj if isinstance(res.obj, dict) else {}
        answer = (obj.get("answer") or res.text or "").strip()
        cited_ids = [c for c in (obj.get("citations") or []) if c in valid_ids]
        answerable = bool(obj.get("answerable", True)) and bool(answer)

        # Show the cited passages; if the model grounded nothing, still surface the top few
        # retrieved passages as "consulted" but flag the answer as ungrounded.
        shown = cited_ids or [p["id"] for p in passages[:3]]
        turn.answer = answer or "I could not find an answer to that in the intelligence archive."
        turn.citations = [_cite(p) for p in passages if p["id"] in shown]
        turn.grounded = bool(cited_ids) and answerable
        turn.model_name = res.model
        turn.token_usage = res.usage or {}
        turn.latency_ms = res.latency_ms
        turn.status = ConversationTurn.Status.READY
        turn.answered_at = timezone.now()
        turn.save()
    except Exception as exc:  # noqa: BLE001 - a turn failure must never crash the worker
        logger.exception("command_intel ask turn %s failed", turn.pk)
        turn.status = ConversationTurn.Status.FAILED
        turn.error = str(exc)[:1000]
        turn.answered_at = timezone.now()
        turn.save(update_fields=["status", "error", "answered_at", "updated_at"])
    return turn


def _degraded(turn: ConversationTurn, passages: list[dict], note: str) -> None:
    """Retrieval-only answer when the LLM is unavailable — still grounded, still gated."""
    lines = [note, ""]
    lines += [f"- [{p['kind']}] {p['title']}" for p in passages[:5]]
    turn.answer = "\n".join(lines) if passages else "The intelligence archive has nothing on that yet."
    turn.citations = [_cite(p) for p in passages[:5]]
    turn.grounded = False
    turn.status = ConversationTurn.Status.READY_DEGRADED
    turn.answered_at = timezone.now()
    turn.save()
