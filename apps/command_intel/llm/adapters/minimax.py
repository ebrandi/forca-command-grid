"""MiniMax adapter — the reference LLM provider (design doc 11 §2, §10).

OpenAI-compatible ``POST {LLM_BASE_URL}/chat/completions`` with ``Authorization:
Bearer``. Encodes the behaviours validated live on 2026-06-30:

* MiniMax-M2.7/M3 are REASONING models that prepend ``<think>…</think>`` to
  ``message.content`` even in JSON mode — stripped here before parsing.
* Success requires ``base_resp.status_code == 0`` in addition to HTTP 200.
* ``finish_reason == "length"`` (reasoning ate the budget) → :class:`LLMTruncated`,
  which the client retries with a larger ``max_output_tokens``.
* Structured output via ``response_format: {"type": "json_object"}``.

The API key is read from settings, attached as a bearer header, and never logged
(``_redact`` scrubs it from any error text). A thin ``requests`` client is used
deliberately (no heavy vendor SDK) to keep the outbound surface auditable.
"""
from __future__ import annotations

import json
import re
import time

import requests
from django.conf import settings

from core import netcap

from ..client import LLMError, LLMRequest, LLMResult, LLMTruncated, LLMUnavailable

# Redact any provider's bearer key: the wide sk- prefix (MiniMax sk-cp-, OpenAI sk- /
# sk-proj- / sk-ant-, …) plus, robustly, the literal configured key value — so a
# non-MiniMax fallback key (4.14) can't slip through into a log / a user-facing degraded
# answer / the persisted ConversationTurn.error.
_SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
# MiniMax envelope codes we treat as transient (retryable) rather than hard errors.
_TRANSIENT_BASE_CODES = {1002, 1039, 1042}  # rate-limit / busy family


def _redact(text: str, api_key: str = "") -> str:
    out = _SK_RE.sub("sk-REDACTED", text or "")
    if api_key and len(api_key) >= 8 and api_key in out:
        out = out.replace(api_key, "***REDACTED-KEY***")
    return out


def _strip_think(content: str) -> str:
    """Return the answer after the model's reasoning block.

    Content is ``<think>…reasoning…</think>\\n\\n{answer}``. Take everything after the
    last ``</think>``. If a think block opened but never closed, the answer never
    arrived (a truncation) — return empty so the caller treats it as such.
    """
    idx = content.rfind("</think>")
    if idx != -1:
        return content[idx + len("</think>"):].strip()
    if "<think>" in content:
        return ""
    return content.strip()


def _parse_json(answer: str) -> dict | None:
    if not answer:
        return None
    try:
        return json.loads(answer)
    except (ValueError, TypeError):
        m = _JSON_OBJ_RE.search(answer)
        if m:
            try:
                return json.loads(m.group(0))
            except (ValueError, TypeError):
                return None
        return None


class MiniMaxAdapter:
    """OpenAI-compatible chat adapter (MiniMax by default). Parameterisable
    base_url/api_key/model so the SAME adapter serves the fallback provider (4.14) — the
    MiniMax-specific ``base_resp`` envelope check is a no-op for a plain OpenAI endpoint
    (absent → status_code None → passes), and ``_strip_think`` is harmless when absent."""

    name = "minimax"

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None):
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.timeout = int(getattr(settings, "LLM_TIMEOUT", 120))
        self.user_agent = getattr(settings, "ESI_USER_AGENT", "FORCA-CommandGrid/CommandIntel")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _messages(self, req: LLMRequest) -> list[dict]:
        msgs = [{"role": "system", "content": req.system}]
        msgs.extend({"role": "user", "content": block} for block in req.user_blocks)
        return msgs

    def generate(self, req: LLMRequest) -> LLMResult:
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": req.model or self.model,
            "messages": self._messages(req),
            "max_tokens": req.max_output_tokens,
            "temperature": req.temperature,
            "response_format": {"type": "json_object"},
        }
        started = time.monotonic()
        try:
            resp = requests.post(
                url, json=body, headers=self._headers(),
                timeout=self.timeout, allow_redirects=False, stream=True,
            )
            # Read the body ONCE under a size cap: the provider host is allowlisted, but a
            # compromised or self-hosted (localhost) LLM could otherwise return a multi-GB
            # reply that requests would buffer whole into the worker's memory (OOM). See
            # core.netcap.read_capped.
            try:
                raw = netcap.read_capped(resp)
            finally:
                resp.close()
        except netcap.DataTooLarge as exc:
            raise LLMUnavailable(f"oversized LLM response: {exc}") from exc
        except requests.RequestException as exc:
            raise LLMUnavailable(f"transport error: {_redact(str(exc), self.api_key)}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        text = raw.decode(resp.encoding or "utf-8", errors="replace")

        if resp.status_code == 429 or resp.status_code >= 500:
            raise LLMUnavailable(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {_redact(text, self.api_key)[:200]}")

        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LLMError(f"non-JSON response: {_redact(text, self.api_key)[:200]}") from exc

        base = data.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (0, None):
            msg = (base.get("status_msg") or "").lower()
            if code in _TRANSIENT_BASE_CODES or "rate" in msg or "busy" in msg:
                raise LLMUnavailable(f"minimax base_resp {code}: {base.get('status_msg')}")
            raise LLMError(f"minimax base_resp {code}: {base.get('status_msg')}")

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("minimax returned no choices")
        choice = choices[0]
        finish = choice.get("finish_reason", "")
        content = (choice.get("message") or {}).get("content") or ""
        answer = _strip_think(content)

        u = data.get("usage") or {}
        usage = {
            "input": u.get("prompt_tokens", 0),
            "output": u.get("completion_tokens", 0),
            "total": u.get("total_tokens", 0),
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        }

        if finish == "length" or not answer:
            raise LLMTruncated(
                "finish_reason=length (reasoning consumed the output budget)",
                partial_text=answer, usage=usage,
            )

        return LLMResult(
            obj=_parse_json(answer),
            text=answer,
            usage=usage,
            model=data.get("model", body["model"]),
            latency_ms=latency_ms,
            finish_reason=finish,
        )
