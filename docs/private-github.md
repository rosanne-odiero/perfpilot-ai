# Private GitHub repositories (design)

Status: **interface only**. `PrivateGitHubProvider.acquire()` raises `NotConfiguredError`
(HTTP 501, code `not_configured`) and `POST /api/repositories` with `source_type=GITHUB_PRIVATE`
is refused before anything is stored. This document is the design the implementation must follow
(ADR-008).

## Goals

* Read-only access to repositories the user explicitly grants, nothing more.
* No long-lived secrets: no personal access tokens, no tokens in URLs, argv, logs, the database,
  job payloads, error messages or reports.
* Same hardened clone path, filters and limits as public repositories.

## Authorization options

1. **GitHub App (preferred).** The operator registers a GitHub App with the single permission
   `contents: read` (plus `metadata: read`). Users install it on the repositories they want to
   audit. PerfPilot stores only the App id and the installation id; the App private key is
   provided through a secret store / environment variable (`PERFPILOT_GITHUB_APP_ID`,
   `PERFPILOT_GITHUB_APP_PRIVATE_KEY` as a `SecretStr`) and never written to the DB.
2. **OAuth device flow (single-user / local mode).** The UI shows the user code and verification
   URL; the worker polls for the token. The resulting user token is kept in memory or the OS
   keychain only, scoped to `repo:read`-equivalent access, and revocable by the user.

## Token handling

* For each ingest the worker exchanges the App JWT for an **installation access token**
  (`POST /app/installations/{id}/access_tokens`), restricted to the single target repository and
  `contents: read`. These tokens expire after one hour; PerfPilot discards them when the job ends.
* The token reaches git **only** through one of:
  * a `GIT_ASKPASS` helper: a small executable in the snapshot directory (mode `0700`) that prints
    the token read from an inherited file descriptor or an environment variable that exists only
    in the git child process; or
  * `http.extraHeader` supplied via `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`
    environment variables (`Authorization: Bearer …`), never via `-c` argv (visible in `ps`).
* The clone URL stays `https://github.com/<owner>/<repo>.git` with no userinfo. The existing
  hardening (`GIT_TERMINAL_PROMPT=0`, no system/global config, `credential.helper=`, hooks
  disabled, HTTPS-only protocol allow-list, no redirects, timeout) applies unchanged.
* Logging: tokens are registered with the structured logger's scrubber, and git stderr is passed
  through `summarize_stderr()` (credential stripping + `redact_text`) before it is stored.
* Persistence: `Repository.source_locator` stores the canonical URL; the installation id is the
  only credential-related value persisted. Snapshots record git metadata with credentials stripped.

## Failure modes

| Situation | Error |
|---|---|
| App not configured | `not_configured` (501) |
| App not installed on the repository / access revoked | `repository_not_found` (404 in the job result) |
| Token exchange fails | `external_service_error` (502) |
| Clone exceeds the timeout | `clone_timeout` (504) |

## Tests required before enabling

* No token appears in argv (`ps`), logs, DB rows, job payloads/results or error envelopes.
* Expired/revoked tokens map to the errors above.
* The askpass helper is removed after the job, including on failure and cancellation.
