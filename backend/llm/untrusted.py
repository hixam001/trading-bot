"""
llm/untrusted.py — one boundary for third-party text that reaches a model.

The bot reads token names, social notes, web-search snippets, crowd theses,
and operator-ingested knowledge. All of it is attacker-controlled in the
memecoin setting. Structural containment (the model may only veto) is the
real defence; this module removes the cheap wins that make an injection
easier to obey:

  * normalise Unicode and strip zero-width/bidi characters;
  * map common Cyrillic/Greek homoglyphs used to hide role words;
  * drop control characters and chat/role markers;
  * remove tag-shaped text so a payload cannot close the untrusted fence;
  * drop instruction-like phrases and long base64 blobs;
  * hard-cap the length of every field.

Nothing here is trusted to make a model obey. Tests in
`test_s5_prompt_injection.py` prove that a model which fully obeys an
injection still cannot change size, mint, caps, stops, or the gate.
"""
from __future__ import annotations

import re
import unicodedata

MAX_FIELD_CHARS = 2000

# Cyrillic/Greek lookalikes that survive NFKC. Mapping them makes the
# instruction-pattern filter work on disguised text and gives the model the
# same plain spelling it would see from an honest author.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "У": "Y",
    "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "ο": "o", "α": "a", "ε": "e", "ι": "i", "ν": "v", "ρ": "p",
    "τ": "t", "υ": "u",
    "Ο": "O", "Α": "A", "Ε": "E", "Ι": "I", "Ν": "N", "Ρ": "P",
    "Τ": "T",
})

_INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u180e]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_TAG = re.compile(r"</?[A-Za-z][^>]{0,300}>")
_CHAT_TOKEN = re.compile(r"<\|[^|]{0,40}\|>")
_INST = re.compile(r"\[/?INST\]", re.IGNORECASE)
_ROLE_LINE = re.compile(
    r"(?im)^\s*(system|assistant|developer|user|tool)\s*:\s*")
_INSTRUCTION = re.compile(
    r"(?i)\b(ignore|disregard|forget|override|bypass)\s+"
    r"(?:(?:all|any|the|this|these|those|your|my|previous|prior|above|"
    r"earlier|system|developer)\s+)*"
    r"(instructions?|rules?|prompts?|directions?|guardrails?|commands?|"
    r"messages?)\b")
_PERSONA = re.compile(
    r"(?i)\b(you are now|from now on you are|act as)\b[^.\n]{0,80}")
_REVEAL = re.compile(
    r"(?i)\b(reveal|print|show|output|repeat|leak)\s+"
    r"(me\s+)?(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions|"
    r"developer\s+message)\b")
_ENCYPTED_BLOB = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/])")


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = " [truncated]"
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[:max_chars - len(suffix)].rstrip() + suffix


def sanitize_untrusted(text, max_chars: int = MAX_FIELD_CHARS) -> str:
    """Return bounded, structurally inert text for a model prompt.

    The output is still *data*: callers must wrap it in `wrap_untrusted` and
    must never concatenate it into a system prompt.
    """
    if text is None:
        return ""
    raw = str(text)
    normalised = unicodedata.normalize("NFKC", raw).translate(_CONFUSABLES)
    normalised = _INVISIBLE.sub("", normalised)
    normalised = _CONTROL.sub(" ", normalised)
    normalised = _TAG.sub(" ", normalised)
    normalised = _CHAT_TOKEN.sub(" ", normalised)
    normalised = _INST.sub(" ", normalised)
    normalised = _ROLE_LINE.sub("[role marker removed] ", normalised)
    normalised = _INSTRUCTION.sub("[instruction-like text removed]", normalised)
    normalised = _REVEAL.sub("[prompt-extraction text removed]", normalised)
    normalised = _PERSONA.sub("[persona text removed]", normalised)
    normalised = _ENCYPTED_BLOB.sub("[encoded blob omitted]", normalised)
    # Never let a payload manufacture our own closing fence.
    normalised = normalised.replace("<untrusted_data", "[tag removed]")
    normalised = normalised.replace("</untrusted_data", "[/tag removed]")
    normalised = re.sub(r"[ \t]{2,}", " ", normalised)
    normalised = re.sub(r"\n{3,}", "\n\n", normalised)
    return _truncate(normalised.strip(), max_chars)


def wrap_untrusted(source: str, text, max_chars: int = 6000) -> str:
    """Fence third-party text with a source label the system prompt explains.

    `source` is an internal constant (never user text); it is sanitised too so
    a future caller cannot inject through the attribute.
    """
    # The label is an attribute, so only a conservative identifier charset
    # is allowed: a future caller cannot inject a quote or close the tag.
    label = re.sub(r"[^A-Za-z0-9_.:-]+", "_",
                   sanitize_untrusted(source, 40)).strip("_") or "data"
    body = sanitize_untrusted(text, max_chars)
    return (
        f'<untrusted_data source="{label}">\n'
        f"{body}\n"
        "</untrusted_data>"
    )
