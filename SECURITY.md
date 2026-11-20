# FrameFrog Pipeline — Security

This document describes the security model **as implemented in this
repository** (branch `feat/gpu-ready-pre-mvp`). It covers what the code
enforces and — just as importantly — what it does not. Where a control is
only partly wired up, that is stated plainly.

Relevant sources: `app/core/security.py`, `app/core/config.py`,
`app/api/deps.py`, `app/core/logging.py`, `app/storage/s3.py`,
`app/media/ffmpeg.py`, `app/jobs/repository.py`.

## 1. Threat model in one paragraph

The service accepts a URI pointing at media, hands it to FFmpeg, and returns
artifacts. The two dominant risks are therefore (a) an unauthenticated or
over-privileged caller using the service as a general-purpose file reader or
compute engine, and (b) the caller steering the pipeline at a target the
server should not reach — a local file outside the job directory, an object
storage key that escapes its prefix, or an internal network address (SSRF).
Everything in §2–§8 addresses those risks. The service runs untrusted input
through FFmpeg, so subprocess handling (§7) is treated as a separate
boundary.

## 2. Authentication

A single shared API key, accepted in either of two headers
(`app/api/deps.py::require_auth`):

```
X-API-Key: <key>
Authorization: Bearer <key>
```

`X-API-Key` wins when both are present. If only `Authorization` is given, the
scheme must be `bearer` (case-insensitive); the token is stripped of
whitespace and used as the key.

**Comparison.** The key is compared with `hmac.compare_digest` over UTF-8
bytes (`app/core/security.py::constant_time_equals`), so the comparison is
constant-time and does not leak the key length or prefix through timing.
`verify_api_key` raises `AuthenticationError` ("missing API key" / "invalid
API key") before any work is done; the handler converts it into a `401`:

```json
{"error":{"code":"authentication_error","message":"missing or invalid API key",
           "details":{"header":"X-API-Key"}}}
```

The submitted key is never echoed back in the response or written to a log
line.

**When authentication is mandatory.** `Settings.auth_required`
(`app/core/config.py`):

| `APP_ENV` | `API_KEY` set? | Authentication |
|---|---|---|
| `development` | no | **not required** |
| `development` | yes | required |
| `test` | any | **required** |
| `production` | any | **required** |

`APP_ENV` is therefore the only way to run an unauthenticated instance, and
that is limited to explicit development mode.

**Startup refusal (production).** The `Settings` model validator refuses to
construct in production when:

1. `API_KEY` is unset or empty — *"API_KEY is mandatory in production;
   refusing to start without it"*;
2. `API_KEY` is one of the known development placeholders — *"API_KEY is a
   known development value; refusing to start"*. The blocklist is
   `INSECURE_API_KEYS = {"", "changeme", "change-me", "secret", "dev",
   "test", "local"}`, compared case-insensitively after stripping.

The same validator also requires `S3_BUCKET` when `STORAGE_BACKEND=s3`.

All job endpoints (`POST /v1/jobs`, `GET /v1/jobs/{job_id}`,
`POST /v1/jobs/{job_id}/cancel`, `GET /v1/jobs/{job_id}/outputs`) depend on
`require_auth`.

**Not protected:** `/health`, `/ready` and `/` are unauthenticated by design.
They expose the version, uptime, `APP_ENV`, the resolved binary versions and
the configured storage bucket name — but no credentials and no job data.

## 3. Input handling

`parse_input_uri` (`app/core/security.py`) accepts exactly three shapes:

| Form | Handling |
|---|---|
| `s3://bucket/key` | bucket and key must both be non-empty; the key passes through `safe_relative_path` |
| `file:///abs/path` or a bare path | resolved and checked against the allowed roots |
| `http(s)://…` | **rejected** unless `allow_http=True` is passed explicitly |

Any other scheme (`ftp://`, `gopher://`, …) raises `ValidationError`:
*"unsupported uri scheme …; allowed: s3, file"*. `ALLOWED_URI_SCHEMES` is
`("s3", "file")`.

**HTTP(S) is disabled by default.** `parse_input_uri(..., allow_http=False)`
is the default and is what every current caller uses — the API route
(`app/api/routes_jobs.py`) and the pipeline (`app/jobs/pipeline.py`) both
call it without `allow_http`. An `http://` or `https://` input therefore
fails with *"http(s) inputs are disabled; upload to object storage or use a
local path"*. There is no configuration flag that turns this on; enabling
HTTP input requires a code change that passes `allow_http=True` — at which
point the SSRF controls in §4 apply.

**Local path containment.** `resolve_local_input(path_like, allowed_roots)`:

1. Empty or whitespace-only input → `ValidationError`.
2. A null byte (`\x00`) anywhere in the path → `ValidationError`
   (*"path contains a null byte"*).
3. The path is made absolute (`~` expanded; relative paths resolved against
   the process CWD).
4. `Path.resolve(strict=False)` is called — **symlinks are resolved before
   the containment check**, so a symlink inside `WORK_DIR` pointing at
   `/etc/passwd` resolves to `/etc/passwd` and is refused.
5. The resolved path must be relative to one of the allowed roots
   (`Path.relative_to`, which raises `ValueError` for paths outside). If no
   root matches → `ValidationError`: *"local input must live under …"*.

The allowed roots for the HTTP API are `WORK_DIR` plus every entry of
`ALLOWED_INPUT_ROOTS` (comma-separated, each `expanduser().resolve()`d).
`ALLOW_EXTERNAL_LOCAL_INPUTS` exists but is only turned on by
`operator_settings()`, which the CLI and `demo.py` use — the HTTP API never
sets it.

**Path traversal.** `safe_relative_path(candidate)` rejects `..` as any path
component (*"path traversal is not allowed"*), absolute paths (*"absolute
paths are not allowed here"*), null bytes and empty input. It is applied to
S3 object keys, so `s3://bucket/../etc/passwd` is refused.

**Object-key sanitising.** `safe_object_key(*parts)` builds every storage key
from untrusted parts: each part is stripped of leading/trailing `/` and
whitespace; backslashes are normalised to `/` and the result is split on
`/`; empty, `.` and `..` segments are dropped; the result can never start
with `/` and can never contain a traversal segment; an empty result raises
`ValidationError` (*"cannot build an empty object key"*).
`StorageBackend.build_key(prefix, *parts)` runs every prefix and part
through it. `LocalStorage.path_for` additionally re-resolves the joined path
and re-checks containment against the storage root, raising `StorageError`
(*"object key escapes the storage root"*) if it ever did.

## 4. SSRF controls

`assert_safe_http_url(url)` is the outbound-request guard. It is exercised
by the unit tests and is the control that applies whenever HTTP input is
explicitly enabled.

1. **Scheme allow-list.** Only `http` and `https`; anything else →
   `ValidationError`.
2. **Host required.** An empty hostname is rejected.
3. **Hostname block-list.** `localhost` and `metadata.google.internal` are
   rejected by name.
4. **DNS resolution of every candidate address.** `socket.getaddrinfo(host,
   port)` is called with the port derived from the scheme (443 for HTTPS, 80
   otherwise). A `socket.gaierror` → *"cannot resolve host …"*; an empty
   answer → *"host … resolved to no address"*.
5. **Every returned address is checked** — not just the first. For each
   address, `_is_disallowed_address` rejects it when it is:
   * one of the cloud metadata endpoints (exact match, checked first):

     | Address | Target |
     |---|---|
     | `169.254.169.254` | AWS / GCP / Azure / DigitalOcean instance metadata |
     | `169.254.170.2` | AWS ECS task metadata |
     | `100.100.100.200` | Alibaba Cloud metadata |
     | `fd00:ec2::254` | AWS IMDSv6 |

   * a loopback, private, link-local, multicast, reserved or unspecified
     address (via `ipaddress.ip_address` predicates);
   * not parseable as an IP at all — a hostname that survives resolution as
     a non-IP is refused, since the caller has no IP to validate.

   A rejection message names the offending address but not the whole
   resolution set: *"host … resolves to a blocked address (…); private,
   loopback, link-local and cloud metadata targets are refused"*.

Because the check is applied to *every* address the hostname maps to, a DNS
name pointing at `127.0.0.1` or at `169.254.169.254` is rejected — not just
the literal forms.

**Limitation, stated plainly:** the check happens at validation time. It does
not pin the resolved address for the subsequent fetch, so a DNS record that
changes between the check and the request is not defended against (no DNS
re-binding protection). It is also not reachable today, because HTTP input is
disabled by default (§3).

## 5. Media validation limits

`app/media/validation.py::validate_media_file` inspects every input with
FFprobe **before** any processing starts. Violations raise
`MediaValidationError`, whose `code` is `media_validation_error` and whose
HTTP status is `422`. The client receives the reason plus the measured value
and the limit:

| Limit | Setting (default) | Reported as |
|---|---|---|
| Input size | `MAX_UPLOAD_BYTES` (2 GiB) | `size_bytes`, `limit_bytes`, `size_human` |
| Input duration | `MAX_VIDEO_DURATION_SECONDS` (3600) | `duration_s`, `limit_s` |
| Input width / height | `MAX_VIDEO_WIDTH` (3840) / `MAX_VIDEO_HEIGHT` (2160) | `width`, `height`, `max_width`, `max_height` |
| Input frame rate | `MAX_OUTPUT_FPS` (60) | `fps`, `max_fps` |

Additional hard checks: the path must exist, be a regular file and be
non-empty; the file must have a video stream; the duration must be
determinable; the frame rate must be determinable and non-zero. A file that
cannot be probed is rejected with *"input file could not be probed"* and the
FFprobe reason (truncated to 400 characters).

The API layer adds a second ceiling on the *requested* output rate:
`validate_target_fps` rejects a `target_fps` above `MAX_OUTPUT_FPS` with
`ValidationError` (422) and details `{"target_fps": …, "max_output_fps": …}`.
The interpolation stage enforces the same 60 fps ceiling independently
(`MAX_TARGET_FPS`), so a job cannot exceed it by bypassing the API.

`collect_violations(info, settings)` returns the full list of violations for
an already-probed file.

## 6. Secret handling

| Secret | Source | Type |
|---|---|---|
| `API_KEY` | environment | `pydantic.SecretStr` |
| `S3_ACCESS_KEY_ID` | environment | `pydantic.SecretStr` |
| `S3_SECRET_ACCESS_KEY` | environment | `pydantic.SecretStr` |

* Secrets are read only from the environment (or `.env`) through
  pydantic-settings. `SecretStr` keeps the plaintext out of `repr()` and out
  of accidental serialisation.
* Secrets are **never logged**. `S3Storage` passes credentials to boto3 as
  constructor arguments and never puts them in a log record; `health()`
  reports the bucket name and an exception *type*, never a credential.
  `app/core/security.py::environment_secret_names()` lists the secret
  variable names (never their values) for operators.
* **Presigned URLs are redacted before logging.** `S3Storage.presign_get` and
  `presign_put` log the URL through `app/core/logging.py::redact_url`, which
  replaces the value of every credential-style query parameter with `***`:
  `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `token`,
  `signature`, `sig`, `key`, `password`, `secret`. The signed URL itself is
  returned to the caller and stored nowhere else.
* **The manifest redacts option secrets.**
  `app/jobs/pipeline.py::_public_options` drops every option whose key
  starts with `_` and replaces the values of `webhook_secret`, `api_key` and
  `s3_secret_access_key` with `***` before the options are written into the
  manifest. The private `_cancel_check` callable therefore never reaches the
  manifest or the database.
* `redact_text(text, secrets)` and `redact_credentials(text, secrets)`
  replace known secret values with `***` in free text.
* `app/media/ffmpeg.py::safe_command` applies the same redaction to command
  lines before they are logged, and truncates any argument longer than 200
  characters.
* `app/core/logging.py::JsonFormatter` writes the envelope `timestamp, level,
  logger, message, event, job_id, stage, duration_seconds, error_code`.
  Values that were not measured are **omitted**, never written as zero.

## 7. Subprocess safety

All external binaries (FFmpeg, FFprobe, `nvidia-smi`) are invoked from
`app/media/ffmpeg.py`, `app/media/ffprobe.py` and `app/core/capabilities.py`.

| Control | Implementation |
|---|---|
| **No shell** | `shell=True` appears nowhere in the repository. Every call is `subprocess.run([...])` with an explicit argument list. |
| **Timeouts** | FFmpeg: `DEFAULT_TIMEOUT = 1800` s (`run(args, timeout=…)` overrides per call). FFprobe: `DEFAULT_TIMEOUT = 120` s. Capability probes: 20 s (`ffmpeg -version`, `nvidia-smi`), 30 s (`ffmpeg -encoders`). A timeout raises `FFmpegError` / `FFprobeError` naming the binary and the limit. |
| **Output checks** | `run_to_file()` verifies that the expected output file exists and that its size is greater than zero; both failures raise `FFmpegError` (*"expected output was not created"*, *"output file is empty"*). |
| **Failure detail** | A non-zero exit raises `FFmpegError` carrying the **last 12 lines of stderr** and the argument list. `app/media/validation.py` truncates the probe reason to 400 characters. |
| **Binary resolution** | `shutil.which(path)` or an explicit file path; a missing binary raises `FFmpegError` (*"binary not found"*) or `CapabilityError` with install instructions (`stages/base.py::require_binary`). |
| **Log safety** | Command lines are only ever recorded through `safe_command()`, which redacts credential-style query parameters and truncates long arguments. |

**Where the stderr tail goes.** The stderr tail is attached to the exception
message, and the pipeline records failed-stage errors as
`f"{type(exc).__name__}: {exc}"` in the manifest stage entry and in
`manifest["error"]["message"]`. That message is returned to the caller in the
`GET /v1/jobs/{job_id}` response, so **the FFmpeg stderr tail is not
confidential** — it is visible to any authenticated caller of that job. It
contains no credentials (FFmpeg is not given any), but it can contain local
file paths and decoder diagnostics. Treat job errors as information that is
disclosed to the API key holder.

## 8. Job isolation and resource bounds

| Control | Implementation |
|---|---|
| **Per-job working directory** | `app/jobs/pipeline.py::_job_paths` creates `WORK_DIR/jobs/<job_id>/work/`. The job id is `fj_<8 hex>` from `uuid4`, so concurrent jobs cannot collide. Every stage writes only inside that directory. |
| **Bounded concurrency** | The runner is a `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)` (default 2). A burst of job-creation requests queues work; it cannot fork an unbounded number of FFmpeg processes. |
| **Cancellation** | `POST /v1/jobs/{job_id}/cancel` sets the `cancel_requested` flag in the row; `repository.request_cancel` refuses to flag a job that already reached a terminal state, and the API answers `409 Conflict` for a finished job. The runner polls the flag between stages, and the interpolation stage polls it per frame. A cancelled job ends in state `cancelled` with error code `job_cancelled`. |
| **No silent overwrite of job state** | `SQLiteJobRepository.update` / `set_status` validate the state transition and raise `InvalidTransitionError` for an illegal edge. Terminal writes go through `save`, which bypasses the check only to guarantee the final state is written. |
| **No silent overwrite of a job id** | `jobs.job_id` is the primary key; a duplicate insert raises `sqlite3.IntegrityError` and is converted to `ConflictError`. |
| **Idempotency** | A partial unique index on `idempotency_key` makes a repeated key a database-level conflict. The API answers `409 Conflict` and returns the existing `job_id` in `details`. The `idempotency_key` charset is restricted to letters, digits, `-`, `_` and `.` (max 200 characters). |
| **Input size** | `MAX_UPLOAD_BYTES` is enforced before any processing (see §5). |

**Storage-side overwrite.** `StorageBackend.publish` uploads to a key derived
from the caller-visible prefix and file name; the S3 backend's `upload_file`
replaces an object with the same key. The pipeline does not call `publish`
today, so no request-path overwrite occurs, but a future caller must
namespace output keys per job (the `outputs` prefix plus the job id is the
intended shape).

## 9. What is **not** protected yet

Honest gaps. None of these are implemented.

| Gap | Status |
|---|---|
| **Per-user accounts** | Not implemented. One shared API key for the whole service; no users, no identity, no per-key scopes, no ownership of a job. Anyone with the key can read any job id. |
| **Quotas / usage limits** | Not implemented. `MAX_CONCURRENT_JOBS` bounds parallelism, not volume. There is no per-key job counter, no byte quota, no rate limit. |
| **Rate limiting** | Not implemented. `app/core/errors.py` defines `RateLimitError` (`rate_limited`, 429) but nothing raises it — there is no middleware, no token bucket, no per-IP limit. |
| **mTLS / client certificates** | Not implemented. Transport security is delegated to a reverse proxy or platform; the application speaks plain HTTP. |
| **SQLite is a single-node store** | The job repository is one SQLite file guarded by an in-process lock. It cannot be shared between hosts, and the docstring's WAL mode is not currently enabled (no `PRAGMA journal_mode=WAL`). Multiple API processes against one file are not a supported topology. |
| **The API does not sign download URLs** | `app/core/ops.py` implements HMAC signing (`sign_download`, `verify_download`, `download_url`) with `FRAMEFROG_SIGNING_SECRET`, but no route uses it: `GET /v1/jobs/{job_id}/outputs` returns **local filesystem paths**, and there is no `download` endpoint. Artifact bytes are therefore not reachable over HTTP at all today. |
| **No audit log of operator actions** | Not implemented. The structured log records job lifecycle events, not who did what. There is no per-actor trail, no administrative action log, and no tamper-evident store. |
| **No encryption at rest configured by the app** | Not implemented. The job database, the manifests and the media files are written in plaintext under `WORK_DIR`; object-storage encryption is whatever the S3 bucket provides. The application configures no KMS, no SSE, no envelope encryption. |
| **No integrity protection for manifests** | The manifest records a SHA-256 per output artifact, but the manifest itself is unsigned and unversioned — it can be edited by anyone with filesystem access. |
| **Webhook and cost plumbing not wired in** | `app/core/ops.py` implements signed webhook delivery and cost accounting and is covered by unit tests, but the pipeline does not call them, so no webhook is fired and no `cost` block appears in the manifest. |
| **DNS rebinding** | Not defended. The SSRF check resolves and validates at validation time and does not pin the address for the fetch (§4). |
| **CORS policy** | Not configured. No `CORSMiddleware` is installed, so the default browser policy applies. |
| **Request body size limit at the HTTP layer** | Not configured. `MAX_UPLOAD_BYTES` is enforced against the *materialised* file, after it has been copied into the job directory. |
| **Timing/response differences on auth failure** | Both "missing" and "invalid" keys produce the same 401 body, and the comparison is constant-time, but no explicit anti-automation throttling exists. |

## 10. Reporting a vulnerability

Please report security issues privately rather than in a public issue.

* **Contact:** `contact@framefrog.me` — the address published in the project
  README.
* **Repository:** <https://github.com/framefrog-ai/framefrog-pipeline> — a
  GitHub Security Advisory ("Report a vulnerability") is the preferred route
  if you have access to it.

What to include:

1. the branch or commit you tested (this branch is `feat/gpu-ready-pre-mvp`);
2. the exact request or command that reproduces the issue, with any real
   credentials replaced by placeholders;
3. the observed behaviour and the expected behaviour;
4. the impact you believe it has (an unauthenticated read of another tenant's
   media, a local file read, an outbound request to an internal address, a
   subprocess injection, and so on);
5. any log lines or manifest excerpts that demonstrate it — **redact
   presigned URLs and API keys**, and note that the query string of a
   presigned URL is a credential.

What to expect: an acknowledgement, an assessment of severity, and a fix or
a mitigation plan. Please give the maintainers a reasonable window to ship a
fix before public disclosure.

Out of scope and worth reporting anyway, but not as vulnerabilities:
performance problems, missing GPU hardware, unsupported codec handling, and
the unimplemented features listed in §9.
