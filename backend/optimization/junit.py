"""Safe parsing of pytest's JUnit XML report (written by code running in the sandbox).

The file is untrusted. Its size is capped before reading, and it is parsed with a bare expat
parser that refuses any document type declaration (so no entity can be declared, expanded or
fetched; parameter entities are disabled as well) and only records ``testcase`` elements, up to a
cap. The result maps a stable test id (``classname::name``) to one outcome.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from pathlib import Path
from xml.parsers import expat

from pydantic import BaseModel

from backend.core.exceptions import ValidationFailedError

MAX_JUNIT_BYTES = 16 * 1024 * 1024
MAX_TESTCASES = 50_000
MAX_ID_CHARS = 1_000


class CaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class JUnitReport(BaseModel):
    outcomes: dict[str, CaseOutcome]
    counts: dict[str, int]

    @property
    def passed(self) -> set[str]:
        return {test for test, outcome in self.outcomes.items() if outcome == CaseOutcome.PASSED}


class _Refused(Exception):
    pass


class _Collector:
    def __init__(self) -> None:
        self.outcomes: dict[str, CaseOutcome] = {}
        self.seen: Counter[str] = Counter()
        self.case: str | None = None
        self.depth = 0
        self.case_depth = 0
        self.child_tags: set[str] = set()

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        local = tag.rsplit("}", 1)[-1]
        if self.case is None and local == "testcase":
            if len(self.outcomes) >= MAX_TESTCASES:
                raise _Refused(f"the JUnit report has more than {MAX_TESTCASES} tests")
            self.case = f"{attrs.get('classname', '')}::{attrs.get('name', '')}"[:MAX_ID_CHARS]
            self.case_depth = self.depth
            self.child_tags = set()
        elif self.case is not None and self.depth == self.case_depth + 1:
            self.child_tags.add(local)

    def end(self, _tag: str) -> None:
        if self.case is not None and self.depth == self.case_depth:
            self.seen[self.case] += 1
            count = self.seen[self.case]
            key = self.case if count == 1 else f"{self.case}#{count}"
            self.outcomes[key] = _outcome(self.child_tags)
            self.case = None
        self.depth -= 1


def _outcome(tags: set[str]) -> CaseOutcome:
    if "error" in tags:
        return CaseOutcome.ERROR
    if "failure" in tags:
        return CaseOutcome.FAILED
    if "skipped" in tags:
        return CaseOutcome.SKIPPED
    return CaseOutcome.PASSED


def _refuse_dtd(*_args: object) -> None:
    raise _Refused("the JUnit report contains a document type declaration; refusing to parse it")


def _refuse_external(*_args: object) -> int:
    raise _Refused("the JUnit report references an external entity; refusing to parse it")


def parse_junit_bytes(data: bytes) -> JUnitReport:
    if len(data) > MAX_JUNIT_BYTES:
        raise ValidationFailedError(f"the JUnit report is larger than {MAX_JUNIT_BYTES} bytes")
    collector = _Collector()
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_dtd
    parser.EntityDeclHandler = _refuse_dtd
    parser.ExternalEntityRefHandler = _refuse_external
    parser.StartElementHandler = collector.start
    parser.EndElementHandler = collector.end
    try:
        parser.Parse(data, True)
    except _Refused as exc:
        raise ValidationFailedError(str(exc)) from exc
    except expat.ExpatError as exc:
        raise ValidationFailedError(f"the JUnit report is not well-formed XML ({exc})") from exc
    counts = Counter(outcome.value for outcome in collector.outcomes.values())
    return JUnitReport(
        outcomes=collector.outcomes,
        counts={o.value: counts.get(o.value, 0) for o in CaseOutcome}
        | {"total": len(collector.outcomes)},
    )


def parse_junit_file(path: Path) -> JUnitReport:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValidationFailedError("the JUnit report is missing")
        if path.stat().st_size > MAX_JUNIT_BYTES:
            raise ValidationFailedError(f"the JUnit report is larger than {MAX_JUNIT_BYTES} bytes")
        data = path.read_bytes()
    except OSError as exc:
        raise ValidationFailedError(f"the JUnit report could not be read ({exc})") from exc
    return parse_junit_bytes(data)
