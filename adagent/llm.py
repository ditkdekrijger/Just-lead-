"""Dunne laag rond de Anthropic SDK: gestructureerde output en tool-loops."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, TypeVar

import anthropic
from pydantic import BaseModel

from .config import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_client: anthropic.Anthropic | None = None


class LLMError(RuntimeError):
    pass


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def structured(system: str, prompt: str, schema: type[T], *, max_tokens: int = 16000) -> T:
    """Eén call met gevalideerde Pydantic-output."""
    try:
        resp = client().beta.messages.parse(
            model=settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": settings.effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        raise LLMError(f"Claude API-fout: {e}") from e
    if resp.stop_reason == "refusal":
        raise LLMError("Claude weigerde dit verzoek")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise LLMError(f"Geen bruikbare output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


ToolHandler = Callable[[str, dict[str, Any]], str]


def run_tool_loop(
    system: str,
    prompt: str,
    tools: list[dict[str, Any]],
    handler: ToolHandler,
    *,
    max_turns: int = 25,
    max_tokens: int = 16000,
) -> str:
    """Handmatige agent-loop: Claude roept tools aan tot hij klaar is. Geeft eindtekst terug.

    De handler voert elke tool uit (met guardrails) en geeft een tekstresultaat terug;
    een handler-exceptie wordt als is_error teruggegeven zodat Claude kan bijsturen.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    for _ in range(max_turns):
        try:
            resp = client().beta.messages.create(
                model=settings.model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": settings.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIError as e:
            raise LLMError(f"Claude API-fout: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMError("Claude weigerde dit verzoek")
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            return "\n".join(b.text for b in resp.content if b.type == "text").strip()

        results = []
        for tu in tool_uses:
            try:
                args = tu.input if isinstance(tu.input, dict) else json.loads(tu.input)
                out = handler(tu.name, args)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            except Exception as e:  # noqa: BLE001 - fout teruggeven aan het model
                log.warning("Tool %s faalde: %s", tu.name, e)
                results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": f"Fout: {e}", "is_error": True}
                )
        messages.append({"role": "user", "content": results})
    raise LLMError("Agent-loop stopte niet binnen het maximum aantal stappen")
