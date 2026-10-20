"""Streaming, size-capped multipart upload of repository ZIP archives.

The request body is parsed incrementally (``python_multipart``) and written straight to a staging
file under the data directory, so an oversized upload is rejected as soon as it crosses
``max_upload_bytes`` instead of being buffered in memory or a temp file first.
"""

from __future__ import annotations

import contextlib
import hashlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING

from fastapi import Request
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool

from backend.core.config import Settings
from backend.core.exceptions import LimitExceededError, ValidationFailedError
from backend.models.database import new_id

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

FILE_FIELD = "file"
TEXT_FIELDS = frozenset({"display_name"})
MAX_TEXT_FIELD_BYTES = 256
_MULTIPART_OVERHEAD = 64 * 1024


@dataclass
class StagedUpload:
    path: Path
    filename: str
    size: int
    sha256: str
    fields: dict[str, str] = field(default_factory=dict)

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


class _UploadSink:
    def __init__(self, staging: Path, max_bytes: int) -> None:
        self.staging = staging
        self.max_bytes = max_bytes
        self.size = 0
        self.hasher = hashlib.sha256()
        self.filename: str | None = None
        self.fields: dict[str, str] = {}
        self._handle: IO[bytes] | None = None
        self._field: str | None = None
        self._buffer = bytearray()
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._in_file = False

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value_cb,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self) -> None:
        self._headers = {}
        self._field = None
        self._in_file = False
        self._buffer.clear()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _header_value_cb(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _header_end(self) -> None:
        self._headers[bytes(self._header_name).lower()] = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def _headers_finished(self) -> None:
        _, params = parse_options_header(self._headers.get(b"content-disposition"))
        name = params.get(b"name", b"").decode("utf-8", errors="replace")
        filename = params.get(b"filename")
        if name == FILE_FIELD and filename is not None:
            if self.filename is not None:
                raise ValidationFailedError("exactly one 'file' part is allowed")
            self.filename = filename.decode("utf-8", errors="replace")
            self._handle = self.staging.open("xb")
            self._in_file = True
        elif filename is not None:
            raise ValidationFailedError(f"unexpected file field {name!r}")
        elif name in TEXT_FIELDS:
            self._field = name
        else:
            raise ValidationFailedError(f"unexpected form field {name!r}")

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._in_file and self._handle is not None:
            self.size += len(chunk)
            if self.size > self.max_bytes:
                raise LimitExceededError(f"upload exceeds the {self.max_bytes}-byte limit")
            self.hasher.update(chunk)
            self._handle.write(chunk)
        elif self._field is not None:
            self._buffer += chunk
            if len(self._buffer) > MAX_TEXT_FIELD_BYTES:
                raise ValidationFailedError(f"form field {self._field!r} is too long")

    def _part_end(self) -> None:
        if self._in_file:
            self.close()
            self._in_file = False
        elif self._field is not None:
            self.fields[self._field] = self._buffer.decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


async def receive_zip_upload(request: Request, settings: Settings) -> StagedUpload:
    """Stream a ``multipart/form-data`` body with one ``file`` part (a ZIP) to a staging file."""
    content_type, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise ValidationFailedError("expected a multipart/form-data upload with a 'file' part")
    declared = request.headers.get("content-length")
    if (
        declared
        and declared.isdigit()
        and int(declared) > settings.max_upload_bytes + _MULTIPART_OVERHEAD
    ):
        raise LimitExceededError(f"upload exceeds the {settings.max_upload_bytes}-byte limit")
    staging_dir = settings.resolved_data_dir / "uploads"
    staging_dir.mkdir(parents=True, exist_ok=True)
    sink = _UploadSink(staging_dir / f"{new_id()}.part", settings.max_upload_bytes)
    parser = MultipartParser(boundary, sink.callbacks())
    try:
        async for chunk in request.stream():
            await run_in_threadpool(parser.write, chunk)
        parser.finalize()
        sink.close()
        if sink.filename is None:
            raise ValidationFailedError("missing 'file' part")
        if not sink.filename.lower().endswith(".zip"):
            raise ValidationFailedError("only .zip archives are accepted")
        if sink.size == 0 or not zipfile.is_zipfile(sink.staging):
            raise ValidationFailedError("upload is not a valid ZIP archive")
    except MultipartParseError as exc:
        sink.close()
        sink.staging.unlink(missing_ok=True)
        raise ValidationFailedError("malformed multipart body") from exc
    except BaseException:
        sink.close()
        with contextlib.suppress(OSError):
            sink.staging.unlink(missing_ok=True)
        raise
    return StagedUpload(
        path=sink.staging,
        filename=sink.filename,
        size=sink.size,
        sha256=sink.hasher.hexdigest(),
        fields=dict(sink.fields),
    )
