# Repository ingestion

Ingestion turns a user-supplied `RepositoryRef` into a **read-only working copy** plus a
`RepositoryInventory` (files, skip reasons, languages, technologies, dependencies, limits hit).
It never imports or executes repository code (ADR-003).

```
POST /api/repositories (JSON) ─┐                       ┌─► provider.acquire ─► workspace/src
POST /api/repositories/upload ─┴─► Repository +        │       (filters + limits while copying)
        (streamed ZIP)              Snapshot(PENDING) ─► job "repository.ingest"
                                                        ├─► build_inventory (hash, classify,
                                                        │     detect, dependencies)
                                                        └─► Snapshot READY | FAILED(error)
```

Code lives in `backend/ingestion/`, orchestration in `backend/services/repository_service.py`,
the job handler in `backend/jobs/handlers/repository.py`, routes in
`backend/api/routes/repositories.py`.

## Providers (`backend/ingestion/providers/`)

Every provider implements `RepositoryProvider.acquire(ref, workspace_dir, settings) ->
AcquiredSource`. `AcquiredSource.skipped`/`limits_hit` carry what the provider filtered out while
acquiring, so all providers produce the **same inventory for the same tree** (tested for local
directory vs. wrapped and flat ZIPs).

| Source | Provider | Notes |
|---|---|---|
| `GITHUB_PUBLIC` | `PublicGitHubProvider` | Only `https://<allowed host>/<owner>/<repo>[.git][/tree/<ref>]`. Hosts from `PERFPILOT_GITHUB_ALLOWED_HOSTS`. |
| `ZIP_UPLOAD` | `ZipUploadProvider` | Archive from the artifact store (kind `repository_upload`). |
| `LOCAL_DIRECTORY` | `LocalDirectoryProvider` | Only real paths inside `PERFPILOT_LOCAL_ALLOWLIST`; copied, never mounted. |
| `GITHUB_PRIVATE` | `PrivateGitHubProvider` | Designed, not enabled: raises `NotConfiguredError` (HTTP 501). See [private-github.md](private-github.md). |

### Public GitHub

* URL validation rejects other schemes (`http`, `ssh`, `git`, `file`, scp-style), userinfo,
  ports, query strings, fragments, percent-encoding, backslashes, whitespace/control characters,
  empty/`.`/`..` segments and other hosts. Malformed owner/repo/ref values fail with 422.
* Refs: branch/tag names (`--branch=<ref>`) or a full 40-hex commit SHA (`git init` + shallow
  `git fetch --depth 1 <url> <sha>` + `checkout --detach FETCH_HEAD`, verified afterwards).
* `git` runs with a fixed executable, an argv list, a timeout (`PERFPILOT_CLONE_TIMEOUT_SECONDS`,
  shared by all commands) that kills the process group, and
  `-c core.hooksPath=/dev/null -c protocol.allow=never -c protocol.https.allow=always
  -c core.symlinks=false -c core.fsmonitor=false -c credential.helper= -c http.followRedirects=false`,
  `--depth 1 --single-branch --no-tags --no-recurse-submodules --template=`.
* Environment: only `PATH`, proxy and CA variables are inherited; `HOME` is the snapshot
  directory, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=`/`SSH_ASKPASS=` (disabled),
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_LFS_SKIP_SMUDGE=1`, `LC_ALL=C`.
* Errors map to typed exceptions: `repository_not_found`, `ref_not_found`, `clone_timeout` (504),
  `external_service_error` (host unreachable), `git_error`. Stderr in error details is
  credential-stripped and redacted.
* `--filter=blob:limit` is deliberately not used: the checkout would lazily fetch the filtered
  blobs anyway. Oversized files are handled by the inventory limits instead.

### ZIP upload

`POST /api/repositories/upload` parses the multipart body incrementally and streams the `file`
part to `<data_dir>/uploads/<id>.part`, aborting with 413 as soon as `PERFPILOT_MAX_UPLOAD_BYTES`
is exceeded (an oversized `Content-Length` is refused before reading). The file must be a `.zip`;
it is stored in the artifact store, deduplicated by sha256. Optional form field: `display_name`.

`safe_extract_zip` validates the whole central directory before writing anything and rejects
(`unsafe_archive`, 400) archives with: absolute paths, drive letters, `..`, backslashes, control
characters, symlink entries, device/FIFO/socket entries, encrypted entries, duplicate names
(case-insensitive), a name that is both file and directory, more than
`PERFPILOT_MAX_ARCHIVE_ENTRIES` entries (413), or a compression ratio above
`PERFPILOT_MAX_COMPRESSION_RATIO` (per entry and archive-wide, applied above 1 MiB). Extraction
streams each entry, counts the bytes actually produced, re-checks the ratio on actual bytes, and
fails if an entry produces more than its declared size (CRC/size mismatches are reported as
unsafe). Files are created with `O_EXCL|O_NOFOLLOW`. A single shared top-level directory (as in
GitHub ZIPs) is unwrapped; `__MACOSX/` is skipped. Git is never run on uploaded content (a
bundled `.git/config` could define hooks or fsmonitor commands), so ZIP snapshots have no git
metadata.

### Local directory

`local_path` must be absolute, must not contain `..`, and its `realpath` must be inside one of
the allowlisted roots (also resolved) and outside the PerfPilot data directory; otherwise
`security_violation` (400). Missing paths return 404. The copy walks the source **with filters and
limits applied during the walk**: pruned directories such as `node_modules/` are never descended
into, symlinks are never followed (recorded as `symlink`), sensitive files are never opened, and
files are copied with `O_NOFOLLOW` and a size cap (files that grow past `max_file_bytes` during
the copy are dropped as `oversized`). Git metadata is read from the source directory.

## Workspaces (`workspace.py`)

`<data_dir>/workspaces/<snapshot_id>/src`. Snapshot ids are validated and the path is checked to
stay inside the workspaces directory. A retry wipes the previous attempt. After a successful
ingest every file and directory is made read-only (best effort); `cleanup_workspace()` restores
write permission and deletes the tree, and is called automatically when ingestion fails.
`resolve_in_workspace()` refuses symlinks and paths that escape `src`.

## Filters (`filters.py`)

Every skipped entry is recorded in the inventory with `skipped=true` and a `skip_reason`. Pruned
directories appear once, with a trailing `/` and size 0.

| Reason | Rule |
|---|---|
| `vcs` | `.git`, `.hg`, `.svn`, `.bzr` |
| `nested_repository` | a subdirectory containing `.git` (submodule, nested clone, worktree) |
| `virtualenv` | `.venv`, `venv`, `site-packages`, any directory containing `pyvenv.cfg` (e.g. `env/`) |
| `node_modules` | `node_modules`, `bower_components` |
| `cache` | `__pycache__`, `.tox`, `.nox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.hypothesis`, … |
| `build_output` | `build`/`dist` (unless they contain `__init__.py`), `.eggs`, `*.egg-info`, `.next`, `.nuxt`, `.svelte-kit`, `.vercel` |
| `coverage` | `coverage`, `htmlcov` |
| `os_metadata` | `__MACOSX` |
| `perfpilot_data` | the PerfPilot data directory, if it lies inside an imported local directory |
| `sensitive` | `backend.core.redaction.is_sensitive_path` (`.env*`, keys, `credentials`, `secrets.*`, …). Never read, hashed, copied or served. |
| `binary` | extension list (images, archives, compiled objects, fonts, data/model blobs, …) or a NUL byte in the first 8 KiB |
| `generated` | `*_pb2.py`, `*_pb2_grpc.py`, source maps, or "generated by" / "DO NOT EDIT" / `@generated` in the first 5 lines. Django/Alembic migrations (`migrations/`, `alembic/**/versions/`) are exempt and analyzed normally. |
| `minified` | `*.min.js`, `*.min.css`, `*.bundle.js`, … or JS/CSS with an average line length > 300 |
| `oversized` | larger than `PERFPILOT_MAX_FILE_BYTES` |
| `symlink`, `special_file`, `unreadable` | non-regular entries and I/O errors |
| `python_file_limit` | Python files beyond `PERFPILOT_MAX_PYTHON_FILES` |

## Limits (`limits.py`)

`max_repo_files` (files seen), `max_repo_bytes` (bytes of analyzed files) stop the walk cleanly;
`max_python_files` skips further Python files but keeps walking; `max_file_bytes` skips single
files. Each reached limit is recorded once in `inventory.limits_hit`, e.g.
`"max_repo_files (20000) reached: stopped early; inventory is partial"`. A partial inventory is a
successful ingest — it is never a crash. The walk order is sorted and depth-first, so partial
results are deterministic.

## Inventory, detection and dependencies

`build_inventory(root, settings, *, prefiltered, limits_hit, root_name)`:

* sha256 per analyzed file (and for content-classified skips); `None` for sensitive files and for
  files skipped by name or size, which are never read;
* language by extension/file name; `languages` counts analyzed files;
* `inventory_content_hash()` hashes `path + sha256` of analyzed files → `RepositorySnapshot.content_hash`.

Detection (`detection.py`) reports `DetectedTechnology` entries with up to 5 evidence strings each:
programming languages (file counts), Python frameworks/libraries from a regex scan of `import` /
`from … import` lines (`imports fastapi in app/main.py:2`) and `async def`, declared dependencies,
`package.json` (Next.js, React, Vue, …), `Dockerfile`/compose files, `alembic.ini`, `manage.py`
with `DJANGO_SETTINGS_MODULE`, `pytest.ini`/`conftest.py`, GitHub Actions workflows, and lock files
(Poetry, Pipenv, uv, PDM, npm, Yarn, pnpm, Bun).

Dependencies (`dependencies.py`) are parsed as data, never executed: `pyproject.toml` (PEP 621
dependencies, `optional:<extra>` groups, PEP 735 `[dependency-groups]`, Poetry main/dev/group
tables), `requirements*.txt`/`*.in` and `requirements/*.txt` (comments, line continuations,
`-r` includes resolved inside the repository only, all other options ignored, URL/path lines
skipped), `setup.cfg`, `setup.py` via `ast` (literal lists/dicts and simple module-level names
only), and `Pipfile`. Names are PEP 503-normalized; URLs are credential-stripped. Lock files are
listed in the snapshot summary (`lock_files`).

## API

| Method | Path | Result |
|---|---|---|
| POST | `/api/repositories` | `RepositoryRef` JSON for `GITHUB_PUBLIC` or `LOCAL_DIRECTORY` → 202 `JobAccepted` (`subject_type="snapshot"`). `GITHUB_PRIVATE` → 501, `ZIP_UPLOAD` → 422. |
| POST | `/api/repositories/upload` | multipart `file` (+ optional `display_name`) → 202 |
| GET | `/api/repositories` | repositories with latest snapshot id/status |
| GET | `/api/repositories/{id}` | + latest snapshot summary (status, git, counts, languages, technologies, dependencies, lock files, limits hit) |
| GET | `/api/snapshots/{id}` | snapshot summary |
| GET | `/api/snapshots/{id}/files` | inventory entries incl. skip reasons; `language`, `python_only`, `include_skipped` filters; 409 until READY |
| GET | `/api/snapshots/{id}/files/content?path=` | redacted text + `redactions` count. Sensitive paths → 403 `sensitive_file` (checked before any lookup); skipped files → 422; unknown → 404; traversal → 400. The bytes are re-hashed and must match the inventory. |

Importing a source that already has a PENDING/INGESTING snapshot returns that snapshot's job
instead of creating a duplicate. Ingest job stages: `acquire` → `inventory` → `finalize`.

## Known limitations

* No disk quota is enforced *while* `git clone` runs; the clone timeout bounds it and the
  inventory limits bound what is analyzed.
* The local-directory dirty check runs `git status` in the operator-allowlisted source directory.
  Hooks and fsmonitor are disabled, but clean/smudge filter drivers configured in that
  repository's own `.git/config` cannot be disabled generically; only allowlist directories you
  trust. Uploaded archives never have git run on them.
