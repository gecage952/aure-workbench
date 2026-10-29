"""Rendering ``sample.md`` for the right rail.

``sample.md`` is the human record, and the scaffolded template puts its worked
examples inside HTML comments so they guide the author without appearing in the
file's own output. Those comments are stripped here for the same reason
:func:`nr_workbench.project.scan._runs_mentioned` strips them: the commented
example lists run numbers and conditions that were never measured, and showing
them beside the real data would be actively misleading.

HTML written into it is rendered as HTML.
"""

from __future__ import annotations

import re

#: HTML comments, including the multi-line ones the template ships.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_comments(text: str) -> str:
    """Remove HTML comments from markdown source.

    Args:
        text: The markdown source.

    Returns:
        The source with comment blocks removed.
    """
    return _COMMENT_RE.sub("", text)


def render(text: str | None) -> str:
    """Render markdown to HTML for display in the rail.

    Args:
        text: The markdown source, or ``None``.

    Returns:
        HTML, or an empty string when there is nothing to render. Falls back to
        an escaped preformatted block if the renderer is unavailable, so a
        missing optional dependency costs formatting and not the page.
    """
    if not text:
        return ""

    source = strip_comments(text).strip()
    if not source:
        return ""

    try:
        from markdown_it import MarkdownIt
    except ImportError:  # pragma: no cover - markdown-it-py is a declared dep
        from html import escape

        return f"<pre>{escape(source)}</pre>"

    # "commonmark" plus tables: the measurement register in sample.md is a
    # pipe table, and rendering it as a wall of pipes would defeat the point.
    parser = MarkdownIt("commonmark", {"html": True, "linkify": False})
    parser.enable("table")
    return str(parser.render(source))
