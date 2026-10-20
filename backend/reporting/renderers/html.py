"""HTML renderer (Jinja2). The same template also produces the HTML that is converted to PDF."""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from backend.reporting.model import NOT_MEASURED_TEXT, SECTION_TITLES, AuditReport
from backend.reporting.renderers.common import format_datetime, humanize

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_LEADING_SPACES = re.compile(r"^ +")
_SPACE_RUNS = re.compile(r" {2,}")


def code_line(text: str) -> Markup:
    """Escape a source line and keep its whitespace for every engine.

    Leading indentation and interior runs of spaces become non-breaking spaces (xhtml2pdf has no
    ``white-space: pre-wrap``); single interior spaces stay breakable so long lines can wrap.
    """
    if not text.strip():
        return Markup("&nbsp;")
    escaped = str(escape(text))
    escaped = _LEADING_SPACES.sub(lambda m: "&nbsp;" * len(m.group()), escaped)
    escaped = _SPACE_RUNS.sub(lambda m: "&nbsp;" * (len(m.group()) - 1) + " ", escaped)
    return Markup(escaped)  # noqa: S704 - built from escape() output above


def _number(value: int | float) -> str:
    return f"{value:,}"


def _fixed(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["code_line"] = code_line
    env.filters["num"] = _number
    env.filters["fixed"] = _fixed
    env.filters["dt"] = format_datetime
    env.filters["human"] = humanize
    return env


def _stylesheet(pdf_engine: str | None) -> Markup:
    names = ["report_print.css", f"page_{pdf_engine}.css"] if pdf_engine else ["report_screen.css"]
    css = "\n".join((TEMPLATES_DIR / name).read_text(encoding="utf-8") for name in names)
    # Our own static assets, not repository-derived content.
    return Markup(css)  # noqa: S704


def _svg_markup(svg: str | None, *, pdf: bool) -> Markup | None:
    """Embed a generated SVG: inline for browsers, as a data-URI image for PDF engines."""
    if not svg:
        return None
    if pdf:
        encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return Markup(
            '<img class="figure-img" src="data:image/svg+xml;base64,{}" alt="{}"/>'
        ).format(encoded, "chart")
    # The SVG is produced by backend.reporting.graph_svg/charts, which escape every label.
    return Markup(svg)  # noqa: S704


def render_html(report: AuditReport, *, pdf_engine: str | None = None) -> str:
    """Render the report. ``pdf_engine`` switches to the print stylesheet for that engine."""
    pdf = pdf_engine is not None
    template = _environment().get_template("report.html.j2")
    return template.render(
        r=report,
        titles=SECTION_TITLES,
        not_measured=NOT_MEASURED_TEXT,
        pdf=pdf,
        pdf_engine=pdf_engine,
        css=_stylesheet(pdf_engine),
        graph_figure=_svg_markup(
            report.architecture.graph.svg if report.architecture.graph else None, pdf=pdf
        ),
        benchmark_chart=_svg_markup(
            report.benchmarks.chart_svg if report.benchmarks else None, pdf=pdf
        ),
    )
