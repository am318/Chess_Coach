"""Optional natural-language narration of a review.

Off by default. The rest of the coach works fully without it; when enabled it
only *explains* engine output (it never chooses moves or overrides evals).

Enable by passing ``--llm`` to ``coach review`` or setting ``COACH_LLM=1`` for
the web app, with ``ANTHROPIC_API_KEY`` in the environment.
"""

from __future__ import annotations

import os
from typing import ClassVar, Protocol


class Narrator(Protocol):
    def explain(self, moment: dict) -> str | None: ...
    def summarise(self, ctx: dict) -> str | None: ...


class NullNarrator:
    """Deterministic, offline phrasing built from the tags - no model call."""

    _PILLAR_TIP: ClassVar[dict[str, str]] = {
        "tactics": "a tactic was available - check every forcing move (checks, captures, threats) first",
        "endgame": "an endgame technique slipped - slow down and calculate to a clear result",
        "positional": "a positional concession - weigh pawn structure, weak squares and piece activity",
        "quiet": "no forcing move here - you needed a plan; improve your worst piece or restrain the opponent",
        "opening": "an opening inaccuracy - review this line's main ideas",
    }

    def explain(self, moment: dict) -> str | None:
        bits = []
        if moment.get("best_san"):
            bits.append(f"{moment['best_san']} was better")
            if moment["best_line_san"]:
                bits.append("(" + " ".join(moment["best_line_san"][:4]) + ")")
        motif = moment.get("motif")
        if motif:
            bits.append(f"motif: {motif}")
        # The pillar tip is a general lesson about a kind of position, and it is
        # only worth saying when there is no more specific lesson to hand. A move
        # tagged `hangs-piece` that the router filed under `quiet` was being told
        # "no forcing move here - you needed a plan" on a position whose actual
        # lesson was that the queen had been put en prise. When a motif fired, it
        # is the lesson.
        tip = self._PILLAR_TIP.get(moment.get("pillar") or "")
        if tip and not motif:
            bits.append("- " + tip)
        return " ".join(bits) or None

    def summarise(self, ctx: dict) -> str | None:
        c = ctx["counts"]
        return (
            f"Average centipawn loss {ctx['acpl']}. "
            f"{c['blunder']} blunders, {c['mistake']} mistakes, {c['inaccuracy']} inaccuracies. "
            f"Biggest leak this game: {ctx['focus'] or 'n/a'}."
        )


class AnthropicNarrator:
    """Thin wrapper. Import is lazy so the package is not a hard dependency."""

    def __init__(self, model: str = "claude-sonnet-5") -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = model
        self._fallback = NullNarrator()

    def _ask(self, prompt: str) -> str | None:
        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=220,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        except Exception:  # noqa: BLE001 - never let narration break a review
            return None

    def explain(self, moment: dict) -> str | None:
        prompt = (
            "You are a chess coach. In 2 sentences, explain to a club player why "
            f"{moment['played_san']} was weak and why {moment.get('best_san')} is better. "
            f"Engine line: {' '.join(moment['best_line_san'])}. "
            f"Theme: {moment.get('pillar')}, motif: {moment.get('motif')}. "
            "Do not invent variations beyond the given line."
        )
        return self._ask(prompt) or self._fallback.explain(moment)

    def summarise(self, ctx: dict) -> str | None:
        prompt = (
            "Summarise this game's lesson for the player in 2-3 sentences. "
            f"ACPL {ctx['acpl']}, counts {ctx['counts']}, result {ctx['result']}, "
            f"opening {ctx['opening']}, biggest leak {ctx['focus']}."
        )
        return self._ask(prompt) or self._fallback.summarise(ctx)


def get_narrator(enabled: bool | None = None) -> Narrator:
    if enabled is None:
        enabled = os.environ.get("COACH_LLM", "") not in ("", "0", "false")
    if enabled and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicNarrator()
        except Exception:  # noqa: BLE001
            return NullNarrator()
    return NullNarrator()
