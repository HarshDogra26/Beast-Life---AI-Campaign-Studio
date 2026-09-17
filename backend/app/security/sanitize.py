"""Treating retrieved web pages as evidence, not instructions.
"""

from __future__ import annotations
import re
import unicodedata
from ..domain.research import detect_injection

MAX_PAGE_CHARS = 6_000
MAX_EXCERPT_CHARS = 700
_SCRIPT_BLOCK = re.compile(r"<(script|style|template)\b.*?</\1>", re.I | re.S)
_HTML_TAG = re.compile(r"<[^>]{0,500}>")
_WHITESPACE = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")

_FENCE_TOKENS = re.compile(
    r"</?untrusted_source[^>]{0,200}>|<\|(?:im_start|im_end|endoftext)\|>",
    re.I,
)

_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def clean_page_text(raw: str, *, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Reduce a fetched page to inert plain text."""
    text = unicodedata.normalize("NFKC", raw or "")
    text = _INVISIBLE.sub("", text)
    text = _SCRIPT_BLOCK.sub(" ", text)
    text = _FENCE_TOKENS.sub("[removed]", text)
    text = _HTML_TAG.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    text = _BLANK_LINES.sub("\n\n", text)
    text = text.strip()

    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " […truncated]"
    return text


def fence(source_id: str, url: str, title: str, body: str) -> str:
    """Quote page content as evidence.
    """
    safe_body = _FENCE_TOKENS.sub("[removed]", body)
    safe_title = _FENCE_TOKENS.sub("[removed]", title).replace('"', "'")[:300]
    return (
        f'<untrusted_source id="{source_id}" url="{url}" title="{safe_title}">\n'
        f"{safe_body}\n"
        f"</untrusted_source>"
    )


def build_evidence_block(sources: list[dict]) -> str:
    """Assemble every fetched page into one fenced evidence block."""
    if not sources:
        return "<no_sources>No pages were successfully retrieved.</no_sources>"
    return "\n\n".join(
        fence(s["id"], s["url"], s["title"], s["text"]) for s in sources
    )


def summarise_flags(text: str) -> list[str]:
    """Named injection heuristics that fired. Recorded, not acted on."""
    return detect_injection(text)


def make_excerpt(text: str, *, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """A short, representative passage for the UI's source card.
    """
    for chunk in (c.strip() for c in text.split("\n")):
        if len(chunk) >= 120:
            return chunk[:max_chars]
    return (text[:max_chars] or "").strip() or "(no extractable text)"
