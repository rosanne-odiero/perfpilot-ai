"""HTML renderer for the experiment report (Jinja2, same visual language as the audit report).

The same template produces the print variant that the PDF engines convert.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from backend.reporting.experiment_charts import samples_box_strip_svg
from backend.reporting.experiment_format import fmt_bytes, fmt_ms, fmt_percent, fmt_ratio
from backend.reporting.experiment_model import EXPERIMENT_SECTION_TITLES, ExperimentReport
from backend.reporting.model import PROVENANCE_LEGEND
from backend.reporting.renderers.common import format_datetime, humanize
from backend.reporting.renderers.html import TEMPLATES_DIR, code_line, pdf_break

MAX_SAMPLE_ROWS = 200


def _plain(text: str, limit: int = 48) -> Markup:
    del limit  # browsers wrap long tokens with CSS
    return Markup.escape(text)


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
    env.filters["fixed"] = _fixed
    env.filters["dt"] = format_datetime
    env.filters["human"] = humanize
    env.filters["ms"] = fmt_ms
    env.filters["pct"] = fmt_percent
    env.filters["ratio"] = fmt_ratio
    env.filters["bytes"] = fmt_bytes
    return env


def _stylesheet(pdf_engine: str | None) -> Markup:
    names = ["report_print.css", f"page_{pdf_engine}.css"] if pdf_engine else ["report_screen.css"]
    css = "\n".join((TEMPLATES_DIR / name).read_text(encoding="utf-8") for name in names)
    # Our own static assets, not experiment-derived content.
    return Markup(css)  # noqa: S704


def _chart(report: ExperimentReport, *, pdf: bool) -> Markup | None:
    svg = samples_box_strip_svg(report.benchmark.samples) if report.benchmark else None
    if not svg:
        return None
    if pdf:
        encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return Markup(
            '<img class="figure-img" src="data:image/svg+xml;base64,{}" alt="{}"/>'
        ).format(encoded, "benchmark samples")
    # Built by experiment_charts from numbers only; the variant labels are escaped there.
    return Markup(svg)  # noqa: S704


def render_experiment_html(report: ExperimentReport, *, pdf_engine: str | None = None) -> str:
    pdf = pdf_engine is not None
    return (
        _environment()
        .get_template("experiment.html.j2")
        .render(
            r=report,
            titles=EXPERIMENT_SECTION_TITLES,
            legend=PROVENANCE_LEGEND,
            pdf=pdf,
            pdf_engine=pdf_engine,
            brk=pdf_break if pdf else _plain,
            css=_stylesheet(pdf_engine),
            chart=_chart(report, pdf=pdf),
            max_sample_rows=MAX_SAMPLE_ROWS,
        )
    )
