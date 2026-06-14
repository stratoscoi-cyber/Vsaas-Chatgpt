"""AI-driven, culturally-aware haggling engine.

Two pieces:

* a deterministic **concession strategy** that decides whether to accept or
  counter and computes a counter price, and
* a **message composer** that phrases the move.

"Cultural nuance" is modelled as *selectable negotiation-style profiles*: tunable
conventions (formality, relationship emphasis, directness, bargaining intensity,
greetings) that a user picks for a thread. They are configurable presets and
fully custom profiles are supported -- they describe negotiation *styles*, not
assumptions about any individual. The composer is pluggable: the built-in
template composer is deterministic and offline; an LLM-backed composer can be
wired in via :func:`make_composer` when an endpoint is configured, without
changing the strategy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Protocol

import requests


@dataclass(frozen=True)
class CultureProfile:
    key: str
    name: str
    description: str
    formality: float            # 0 casual .. 1 highly formal
    relationship_emphasis: float  # 0 transactional .. 1 relationship-first
    directness: float           # 0 indirect/softened .. 1 blunt
    haggle_intensity: float     # 0 little bargaining .. 1 expressive bargaining
    typical_rounds: int
    greeting: str
    closing: str

    def to_dict(self) -> Dict:
        return asdict(self)


# Respectful, configurable presets. Names describe a *style*; any regional
# association is a soft convention, and users may select or customise freely.
PROFILES: Dict[str, CultureProfile] = {
    "direct": CultureProfile(
        key="direct", name="Direct / transactional",
        description="Concise, price-focused, minimal small talk.",
        formality=0.4, relationship_emphasis=0.15, directness=0.9,
        haggle_intensity=0.2, typical_rounds=2,
        greeting="Hello,", closing="Best regards,",
    ),
    "relationship_first": CultureProfile(
        key="relationship_first", name="Relationship-first",
        description="Warm, trust-building tone before discussing numbers.",
        formality=0.6, relationship_emphasis=0.85, directness=0.45,
        haggle_intensity=0.5, typical_rounds=4,
        greeting="Greetings, and I hope you and your family are well.",
        closing="With respect and looking forward to working together,",
    ),
    "high_context_formal": CultureProfile(
        key="high_context_formal", name="Formal / high-context",
        description="Very polite and indirect; patience and face-saving matter.",
        formality=0.95, relationship_emphasis=0.7, directness=0.25,
        haggle_intensity=0.5, typical_rounds=4,
        greeting="Esteemed partner, thank you for your kind consideration.",
        closing="Respectfully yours,",
    ),
    "market_bargaining": CultureProfile(
        key="market_bargaining", name="Open-market bargaining",
        description="Expressive, lively haggling with a wide opening gap.",
        formality=0.35, relationship_emphasis=0.6, directness=0.6,
        haggle_intensity=0.85, typical_rounds=6,
        greeting="My friend, welcome — let us find a good price together.",
        closing="Looking forward to a deal that makes us both happy,",
    ),
    "consensus": CultureProfile(
        key="consensus", name="Collaborative / consensus",
        description="Cooperative, fairness-oriented, moderate pace.",
        formality=0.6, relationship_emphasis=0.6, directness=0.5,
        haggle_intensity=0.4, typical_rounds=3,
        greeting="Hello, thank you for the opportunity to work together.",
        closing="Kind regards,",
    ),
}

DEFAULT_PROFILE_KEY = "consensus"


def get_profile(key: Optional[str]) -> CultureProfile:
    return PROFILES.get(key or DEFAULT_PROFILE_KEY, PROFILES[DEFAULT_PROFILE_KEY])


def build_profile(spec: Dict) -> CultureProfile:
    """Construct a custom profile, filling gaps from the consensus preset."""
    base = PROFILES[DEFAULT_PROFILE_KEY].to_dict()
    base.update({k: v for k, v in spec.items() if k in base})
    base["key"] = spec.get("key", "custom")
    base["name"] = spec.get("name", "Custom")
    base["description"] = spec.get("description", "User-defined negotiation style.")
    return CultureProfile(**base)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class NegotiationMove:
    action: str            # "accept" | "counter"
    counter_price: Optional[float]
    rationale: str
    round: int
    style: str

    def to_dict(self) -> Dict:
        return {
            "action": self.action,
            "counter_price": round(self.counter_price, 2) if self.counter_price is not None else None,
            "rationale": self.rationale,
            "round": self.round,
            "style": self.style,
        }


def decide_move(
    *,
    my_target: float,
    their_price: float,
    role: str,
    round_index: int,
    profile: CultureProfile,
    accept_threshold: float = 0.03,
) -> NegotiationMove:
    """Decide whether to accept ``their_price`` or counter, and at what price.

    ``role`` is ``"shipper"`` (wants a lower price) or ``"bidder"`` (wants a
    higher price). ``my_target`` is the price this party would be content with.
    """
    wants_lower = role == "shipper"
    already_good = their_price <= my_target if wants_lower else their_price >= my_target
    within_threshold = abs(their_price - my_target) <= accept_threshold * max(1.0, abs(my_target))
    if already_good or within_threshold:
        return NegotiationMove(
            action="accept", counter_price=their_price, round=round_index, style=profile.key,
            rationale="Their price meets or beats the target; accepting is optimal.",
        )

    spread = abs(their_price - my_target)
    # Anchor a little beyond the target (more for high-intensity styles) to leave
    # room to concede.
    direction = 1.0 if my_target < their_price else -1.0  # toward 'better for me' is opposite their price
    overshoot = profile.haggle_intensity * 0.5 * spread
    anchor = my_target - direction * overshoot  # step further from their price

    rounds = max(1, profile.typical_rounds)
    fraction = _clamp((round_index / rounds) ** (0.5 + profile.haggle_intensity))
    counter = anchor + (their_price - anchor) * fraction

    # Never propose a price worse for us than their current offer.
    if wants_lower:
        counter = min(counter, their_price)
    else:
        counter = max(counter, their_price)

    rationale = (
        f"Round {round_index} of ~{rounds} in a '{profile.name}' style; "
        f"conceding {round(fraction * 100)}% of the gap toward their price."
    )
    return NegotiationMove(
        action="counter", counter_price=counter, round=round_index, style=profile.key, rationale=rationale
    )


class MessageComposer(Protocol):
    def compose(self, profile: CultureProfile, role: str, move: NegotiationMove,
                currency: str, context: Optional[Dict] = None) -> str: ...


class TemplateComposer:
    """Deterministic, offline composer that adapts tone to the profile and the
    requested language (``context['lang']``), via the i18n catalog."""

    def compose(self, profile, role, move, currency, context=None) -> str:
        from ..common import i18n

        context = context or {}
        lang = context.get("lang", i18n.DEFAULT_LANGUAGE)
        name = context.get("counterparty_name")
        warm = profile.relationship_emphasis >= 0.6
        money = f"{currency} {move.counter_price:,.2f}" if move.counter_price is not None else currency
        lines: List[str] = []

        greeting = i18n.translate("greeting.warm" if warm else "greeting.default", lang)
        if name:
            greeting = f"{greeting.rstrip(' ,،.')}, {name}."
        lines.append(greeting)

        if warm:
            lines.append(i18n.translate("relationship.line", lang))

        if context.get("load_title"):
            lines.append(i18n.translate("load.line", lang, title=context["load_title"]))

        if move.action == "accept":
            body = i18n.translate("accept.line", lang, price=money)
        else:
            tier = "offer.direct" if profile.directness >= 0.7 else (
                "offer.soft" if profile.directness >= 0.45 else "offer.indirect")
            body = i18n.translate(tier, lang, price=money)
            if profile.haggle_intensity >= 0.7:
                body += " " + i18n.translate("room.line", lang)
        lines.append(body)

        if context.get("fair_rate") and profile.directness < 0.7:
            fair = f"{currency} {float(context['fair_rate']):,.2f}"
            lines.append(i18n.translate("fair.reference", lang, price=fair))

        lines.append(i18n.translate("closing.warm" if warm else "closing.default", lang))
        return "\n".join(lines)


class HttpHaggleComposer:
    """LLM-backed composer: forwards the move + profile to an inference endpoint."""

    def __init__(self, endpoint_url: str, timeout: float = 30.0):
        self.endpoint_url = endpoint_url
        self.timeout = timeout
        self._fallback = TemplateComposer()

    def compose(self, profile, role, move, currency, context=None) -> str:
        try:
            resp = requests.post(
                self.endpoint_url,
                json={
                    "profile": profile.to_dict(),
                    "role": role,
                    "move": move.to_dict(),
                    "currency": currency,
                    "context": context or {},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            message = resp.json().get("message")
            if message:
                return message
        except (requests.RequestException, ValueError):
            pass
        # Degrade gracefully to the deterministic composer.
        return self._fallback.compose(profile, role, move, currency, context)


def make_composer(endpoint_url: Optional[str]) -> MessageComposer:
    return HttpHaggleComposer(endpoint_url) if endpoint_url else TemplateComposer()
