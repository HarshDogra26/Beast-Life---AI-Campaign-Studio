"""Prompt loading.

Every prompt lives in its own ``.md`` file in this package rather than inline in
Python, so they are easy to find, diff and revise without reading orchestration
code. Placeholders use ``$name`` (``string.Template``) rather than ``str.format``
because prompt bodies contain JSON braces, which ``format`` would choke on.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

_PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=32)
def _read(name: str) -> str:
    path = _PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        available = sorted(p.stem for p in _PROMPT_DIR.glob("*.md"))
        raise FileNotFoundError(f"no prompt named {name!r}; available: {available}")
    return path.read_text(encoding="utf-8").strip()


def render(name: str, /, **values: object) -> str:
    """Render a prompt, failing loudly on a missing placeholder.

    ``substitute`` (not ``safe_substitute``) is deliberate: a silently unfilled
    ``$placeholder`` reaching a model is a bug that produces plausible-looking
    but wrong output, which is the expensive kind to notice late.
    """
    template = Template(_read(name))
    return template.substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def raw(name: str, /) -> str:
    """A prompt with no placeholders."""
    return _read(name)


def available() -> list[str]:
    return sorted(p.stem for p in _PROMPT_DIR.glob("*.md"))
