"""HTML the pages share: the layout, the one stylesheet, tables, links, forms and paging.

Every value is escaped unless it is ``Markup``, which only these helpers and the
report and statistics renderers (which escape for themselves) produce.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode

from benchrun import __version__
from benchrun.reports.html_report import STYLE, _badge, _esc, _num
from benchrun.stats.charts import EXTRA_STYLE, percent

WEB_STYLE = """
body { max-width: 80rem; }
a { color: var(--accent); }
nav.top { display: flex; flex-wrap: wrap; gap: .25rem 1rem; align-items: baseline; padding-bottom: .5rem;
          margin-bottom: 1.25rem; border-bottom: 1px solid var(--line); }
nav.top a { text-decoration: none; }
nav.top a.here { color: var(--ink); font-weight: 600; }
nav.top .name { font-weight: 700; margin-right: .5rem; }
form.filters { display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: flex-end; margin: .5rem 0 1rem; }
form.filters label, form.launch label { display: flex; flex-direction: column; font-size: .85rem; color: var(--muted); }
form.launch { display: grid; gap: .75rem; max-width: 32rem; }
form.inline { display: inline; }
label.check { flex-direction: row; gap: .4rem; align-items: center; }
input, select, button { font: inherit; padding: .25rem .45rem; border: 1px solid var(--line); border-radius: .25rem;
                        background: #fbfcfe; color: var(--ink); }
button { background: var(--accent); border-color: var(--accent); color: #fbfcfe; cursor: pointer; }
button.stop { background: var(--fail); border-color: var(--fail); }
.links { display: flex; flex-wrap: wrap; gap: .25rem 1rem; margin: .5rem 0; }
.pager { display: flex; gap: 1rem; margin: .75rem 0; }
.problems { border-left: 4px solid var(--fail); padding: .5rem .75rem; background: var(--band); margin: 1rem 0; }
pre.output { background: #1d2433; color: #e8ecf3; padding: .75rem; overflow-x: auto; white-space: pre-wrap;
             font: 13px/1.4 ui-monospace, Consolas, monospace; }
footer { margin-top: 3rem; color: var(--muted); font-size: .85rem; }
"""

STYLESHEET = STYLE + EXTRA_STYLE + WEB_STYLE

_NAV = (
    ("dashboard", "/", "Dashboard"),
    ("runs", "/runs", "Runs"),
    ("plans", "/plans", "Plans"),
    ("stats", "/stats", "Statistics"),
    ("lots", "/lots", "Lots"),
    ("stations", "/stations", "Stations"),
    ("instruments", "/instruments", "Instruments"),
    ("products", "/products", "Products"),
    ("audit", "/audit", "Audit log"),
)


class Markup(str):
    """Text that is already HTML. Anything else is escaped wherever it is put."""


def esc(value: Any) -> Markup:
    return value if isinstance(value, Markup) else Markup(_esc(value))


def join(parts: Iterable[Any], separator: str = "") -> Markup:
    return Markup(separator.join(esc(part) for part in parts))


def url(path: str, params: Optional[Mapping[str, Any]] = None) -> str:
    kept = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    return path + ("?" + urlencode(kept) if kept else "")


def item_target(base: str, value: str, key: str) -> Tuple[str, Dict[str, str]]:
    """The path and query naming one thing, such as a serial. A name holding a slash goes in the
    query, because the server refuses encoded slashes in a path."""
    if "/" in value or "\\" in value:
        return base, {key: value}
    return f"{base}/{quote(value, safe='')}", {}


def item_url(base: str, value: str, key: str) -> str:
    return url(*item_target(base, value, key))


def link(href: str, text: Any) -> Markup:
    return Markup(f'<a href="{_esc(href)}">{esc(text)}</a>')


def badge(status: str) -> Markup:
    return Markup(_badge(status))


def number(value: Optional[float]) -> str:
    return _num(value)


def share(value: Optional[float]) -> str:
    return percent(value)


def when(text: Optional[str]) -> str:
    """A stored UTC time as people read it, to the second."""
    return (text or "")[:19].replace("T", " ")


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]], numeric: Sequence[int] = ()) -> Markup:
    def cell(tag: str, index: int, value: Any) -> str:
        css = ' class="num"' if index in numeric else ""
        return f"<{tag}{css}>{esc('' if value is None else value)}</{tag}>"

    head = "".join(cell("th", i, h) for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(row)) + "</tr>" for row in rows)
    return Markup(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def facts(rows: Iterable[Tuple[str, Any]]) -> Markup:
    """A two-column table of labels and values, leaving out empty values."""
    body = "".join(f"<tr><th>{esc(label)}</th><td>{esc(value)}</td></tr>" for label, value in rows if value not in (None, ""))
    return Markup(f'<div class="scroll"><table><tbody>{body}</tbody></table></div>')


def problems(found: Sequence[str]) -> Markup:
    items = "".join(f"<li>{esc(problem)}</li>" for problem in found)
    return Markup(f'<div class="problems"><strong>Cannot continue:</strong><ul>{items}</ul></div>')


def text_input(name: str, label: str, value: Optional[str], kind: str = "text", required: bool = False) -> Markup:
    extra = " required" if required else ""
    return Markup(
        f'<label>{esc(label)}<input type="{kind}" name="{_esc(name)}" value="{_esc(value or "")}" maxlength="200"{extra}></label>'
    )


def select(name: str, label: str, options: Iterable[Tuple[Any, Any]], chosen: Optional[str], blank: str = "any") -> Markup:
    items = [f'<option value="">{esc(blank)}</option>'] if blank is not None else []
    for value, text in options:
        mark = " selected" if chosen is not None and str(value) == chosen else ""
        items.append(f'<option value="{_esc(value)}"{mark}>{esc(text)}</option>')
    return Markup(f'<label>{esc(label)}<select name="{_esc(name)}">{"".join(items)}</select></label>')


def hidden(name: str, value: str) -> Markup:
    return Markup(f'<input type="hidden" name="{_esc(name)}" value="{_esc(value)}">')


def filter_form(action: str, fields: Iterable[Markup], keep: Optional[Mapping[str, str]] = None) -> Markup:
    kept = "".join(hidden(name, value) for name, value in (keep or {}).items() if value)
    return Markup(f'<form class="filters" method="get" action="{_esc(action)}">{"".join(fields)}{kept}<button type="submit">Show</button></form>')


def post_button(action: str, text: str, token: str, css: str = "") -> Markup:
    style = f' class="{_esc(css)}"' if css else ""
    return Markup(
        f'<form class="inline" method="post" action="{_esc(action)}">{hidden("token", token)}'
        f'<button type="submit"{style}>{esc(text)}</button></form>'
    )


def pager(path: str, params: Mapping[str, Any], paging: Dict[str, Any]) -> Markup:
    page = paging["page"]
    parts = []
    if page > 1:
        parts.append(link(url(path, {**params, "page": page - 1}), "Newer"))
    if "pages" in paging:
        parts.append(Markup(f'<span class="muted">Page {page} of {paging["pages"]}, {paging["total"]} in all</span>'))
    if paging["has_more"]:
        parts.append(link(url(path, {**params, "page": page + 1}), "Older"))
    return Markup(f'<div class="pager">{"".join(parts)}</div>')


def page(title: str, body: Any, here: str = "", runs_allowed: bool = False, refresh_s: Optional[int] = None) -> str:
    nav = [Markup('<span class="name">benchrun</span>')]
    entries = _NAV + ((("launch", "/launch", "Start a run"),) if runs_allowed else ())
    for key, href, text in entries:
        css = ' class="here"' if key == here else ""
        nav.append(Markup(f'<a href="{href}"{css}>{esc(text)}</a>'))
    refresh = f'<meta http-equiv="refresh" content="{int(refresh_s)}">' if refresh_s else ""
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}'
        f'<title>{esc(title)}: benchrun</title><link rel="stylesheet" href="/static/style.css"></head><body>'
        f'<nav class="top">{"".join(nav)}</nav>{esc(body)}'
        f"<footer>benchrun {esc(__version__)}. Times are UTC.</footer></body></html>\n"
    )
