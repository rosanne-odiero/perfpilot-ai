"""Frontend test support: a fake API serving JSON captured from the real API.

Fixtures in ``tests/frontend/fixtures/`` were recorded with ``scripts/capture_ui_fixtures.py``;
``manifest.json`` maps request paths to files. The fake replaces ``frontend.api_client.request``
and ``download``, so pages run unchanged against realistic responses without a server.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from frontend.api_client import ENDPOINT_MISSING, ApiError, Download

FIXTURES = Path(__file__).parent / "frontend" / "fixtures"
APP = str(Path(__file__).resolve().parents[1] / "frontend" / "app.py")

# Routes served by API areas that are not part of this API version.
MISSING_PREFIXES = ("/runtime/", "/ai/", "/optimizations", "/analyses")
MISSING_SUFFIXES = ("/recommendations", "/insights", "/llm-usage", "/runtime/commands", "/runtime")


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text("utf-8"))


class FakeApi:
    def __init__(self) -> None:
        manifest: dict[str, str] = load("manifest.json")
        self.routes = {key: load(name) for key, name in manifest.items()}
        self.posts: list[tuple[str, Any]] = []
        self.post_responses: dict[str, Any] = {}
        self.overrides: dict[str, Any] = {}

    def _missing(self, path: str) -> bool:
        return path.startswith(MISSING_PREFIXES) or (
            path.startswith("/audits/") and path.endswith(MISSING_SUFFIXES)
        )

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if path in self.overrides:
            return copy.deepcopy(self.overrides[path])
        if path == "/jobs":
            jobs = self.routes["/jobs"]
            wanted = params.get("job_type")
            return [j for j in jobs if wanted is None or j["job_type"] == wanted]
        if path.startswith("/jobs/"):
            job_id = path.removeprefix("/jobs/")
            for job in self.routes["/jobs"]:
                if job["id"] == job_id:
                    return copy.deepcopy(job)
        if path.endswith("/files/content"):
            path = f"{path}?path={params['path']}"
        if path in self.routes:
            return copy.deepcopy(self.routes[path])
        if self._missing(path):
            raise ApiError(404, ENDPOINT_MISSING, f"GET /api{path} is not available")
        raise ApiError(404, "not_found", f"{path} not found")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET":
            return self._get(path, dict(kwargs.get("params") or {}))
        self.posts.append((path, kwargs.get("json")))
        if self._missing(path):
            raise ApiError(404, ENDPOINT_MISSING, f"{method} /api{path} is not available")
        if path in self.post_responses:
            response = self.post_responses[path]
            if isinstance(response, ApiError):
                raise response
            return copy.deepcopy(response)
        raise ApiError(422, "validation_failed", f"unexpected POST {path}")

    def download(self, path: str, **_: Any) -> Download:
        report_id = path.split("/")[2]
        return Download(f"report {report_id}".encode(), "report.bin", "application/octet-stream")
