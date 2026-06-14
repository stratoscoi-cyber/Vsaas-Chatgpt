"""Internationalisation: language catalog, translation and locale resolution.

The catalog focuses on the phrases the platform actually emits to end users —
chiefly the negotiation/haggling building blocks — across languages common across
Africa: English, French, Portuguese, Swahili, Arabic and Hausa. Translation is
**fallback-safe**: a missing key in a language falls back to the base language and
then to English, so partial catalogs never produce broken output (and never
fabricate a translation). New languages/keys can be added without code changes.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Languages the platform can negotiate to. ``name`` is the endonym/English label;
# ``rtl`` marks right-to-left scripts for the frontend.
LANGUAGES: Dict[str, Dict] = {
    "en": {"name": "English", "rtl": False},
    "fr": {"name": "Français", "rtl": False},
    "pt": {"name": "Português", "rtl": False},
    "sw": {"name": "Kiswahili", "rtl": False},
    "ar": {"name": "العربية", "rtl": True},
    "ha": {"name": "Hausa", "rtl": False},
}
DEFAULT_LANGUAGE = "en"

# Translation catalog. English is complete; other languages provide the phrases
# we are confident in and fall back to English for the rest.
CATALOG: Dict[str, Dict[str, str]] = {
    "en": {
        "greeting.default": "Hello,",
        "greeting.warm": "Greetings, I hope you and your family are well.",
        "relationship.line": "Thank you for engaging with us — a fair, lasting partnership matters more to us than any single trip.",
        "load.line": "Regarding the shipment “{title}”:",
        "offer.direct": "We can do {price}.",
        "offer.soft": "Could we meet around {price}?",
        "offer.indirect": "If agreeable, perhaps we might consider a figure around {price}.",
        "accept.line": "Your terms work for us — we are glad to accept {price}.",
        "room.line": "There is a little room to move if we can settle today.",
        "fair.reference": "For reference, a fair market rate for this load is about {price}.",
        "closing.default": "Best regards,",
        "closing.warm": "With respect and looking forward to working together,",
    },
    "fr": {
        "greeting.default": "Bonjour,",
        "greeting.warm": "Bonjour, j’espère que vous et votre famille allez bien.",
        "relationship.line": "Merci d’échanger avec nous — un partenariat juste et durable compte plus pour nous qu’un seul trajet.",
        "load.line": "Concernant l’expédition « {title} » :",
        "offer.direct": "Nous pouvons faire {price}.",
        "offer.soft": "Pourrions-nous nous accorder autour de {price} ?",
        "offer.indirect": "Si cela vous convient, nous pourrions envisager un montant autour de {price}.",
        "accept.line": "Vos conditions nous conviennent — nous acceptons volontiers {price}.",
        "room.line": "Il reste une petite marge si nous concluons aujourd’hui.",
        "fair.reference": "À titre indicatif, un tarif équitable pour ce chargement est d’environ {price}.",
        "closing.default": "Cordialement,",
        "closing.warm": "Avec respect, au plaisir de travailler ensemble,",
    },
    "pt": {
        "greeting.default": "Olá,",
        "greeting.warm": "Olá, espero que você e a sua família estejam bem.",
        "relationship.line": "Obrigado por negociar connosco — uma parceria justa e duradoura importa-nos mais do que uma única viagem.",
        "load.line": "Sobre o carregamento “{title}”:",
        "offer.direct": "Podemos fazer {price}.",
        "offer.soft": "Podemos chegar perto de {price}?",
        "offer.indirect": "Se concordar, talvez possamos considerar um valor próximo de {price}.",
        "accept.line": "As suas condições servem-nos — aceitamos com gosto {price}.",
        "room.line": "Há uma pequena margem se fecharmos hoje.",
        "fair.reference": "A título de referência, uma tarifa justa para esta carga é cerca de {price}.",
        "closing.default": "Atenciosamente,",
        "closing.warm": "Com respeito e na expectativa de trabalharmos juntos,",
    },
    "sw": {
        "greeting.default": "Habari,",
        "greeting.warm": "Habari, natumai wewe na familia yako mko salama.",
        "relationship.line": "Asante kwa kujadiliana nasi — ushirikiano wa haki na wa kudumu ni muhimu zaidi kwetu kuliko safari moja.",
        "load.line": "Kuhusu mzigo “{title}”:",
        "offer.direct": "Tunaweza kufanya {price}.",
        "offer.soft": "Tunaweza kukubaliana karibu na {price}?",
        "offer.indirect": "Kama unakubali, tunaweza kuzingatia kiasi cha karibu {price}.",
        "accept.line": "Masharti yako yanatufaa — tunakubali {price}.",
        "room.line": "Kuna nafasi kidogo tukimaliza leo.",
        "fair.reference": "Kwa kumbukumbu, bei ya haki kwa mzigo huu ni takriban {price}.",
        "closing.default": "Wako,",
        "closing.warm": "Kwa heshima, natarajia kufanya kazi pamoja,",
    },
    "ar": {
        "greeting.default": "مرحبًا،",
        "greeting.warm": "مرحبًا، أتمنى أن تكون أنت وعائلتك بخير.",
        "closing.default": "مع خالص التحية،",
    },
    "ha": {
        "greeting.default": "Sannu,",
        "greeting.warm": "Sannu, ina fatan kai da iyalinka lafiya.",
    },
}


def supported_languages() -> List[Dict]:
    return [{"code": code, **meta} for code, meta in LANGUAGES.items()]


def is_supported(lang: Optional[str]) -> bool:
    return bool(lang) and lang in LANGUAGES


def base_lang(lang: str) -> str:
    """``en-GB`` -> ``en``."""
    return lang.split("-")[0].split("_")[0].strip().lower()


def translate(key: str, lang: str = DEFAULT_LANGUAGE, **params) -> str:
    """Translate ``key`` into ``lang`` with ``{param}`` substitution.

    Falls back: requested language -> base language -> English -> the key itself.
    """
    for candidate in (lang, base_lang(lang), DEFAULT_LANGUAGE):
        table = CATALOG.get(candidate)
        if table and key in table:
            text = table[key]
            break
    else:
        text = key
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError):
        return text


def resolve_language(
    explicit: Optional[str] = None,
    accept_language: Optional[str] = None,
    region_language: Optional[str] = None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """Pick the best supported language.

    Priority: explicit request (``?lang=``) > ``Accept-Language`` header > the
    region's primary language > platform default.
    """
    if explicit:
        b = base_lang(explicit)
        if is_supported(b):
            return b
    if accept_language:
        for part in accept_language.split(","):
            tag = base_lang(part.split(";")[0])
            if is_supported(tag):
                return tag
    if region_language and is_supported(base_lang(region_language)):
        return base_lang(region_language)
    return default if is_supported(default) else DEFAULT_LANGUAGE
