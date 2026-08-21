# Authentication and identity boundaries

[English](authentication.md) | [简体中文](authentication.zh-CN.md)

AEF-GRiTS follows one invariant: **downloads only reuse credentials that
already exist; a download, retry, resume, or Web worker never starts a login
flow**. Interactive authorization is available only through the explicit
`aef-grits-auth login` command or the Web application's login button.

## Three separate identities

1. The application user controls who can see and manage a Web task.
2. Google/Earth Engine credentials control whose data access is used.
3. The Cloud Project controls quota, API enablement, and request attribution.

A Google login does not remove the Project requirement. Each user must select
a Project they can use and on which the Earth Engine API is enabled. Project
IDs are deployment metadata, not OAuth secrets, but AEF-GRiTS does not embed
them in source code, public task data, or logs.

## CLI credential discovery

All download commands accept `--auth-source auto|earthengine|adc` and an
optional `--project`. `auto` uses this deterministic order:

1. an explicit `GOOGLE_APPLICATION_CREDENTIALS` configuration, if present;
2. the current user's Earth Engine persistent credentials;
3. local ADC or an attached cloud service account.

An explicit invalid source fails closed. It never falls back to another user.
Project resolution uses `--project`, then the private runtime
`AEF_GRITS_PROJECT`, then a Project/quota Project already stored with the
selected credentials. No real Project is committed as a default.

Inspect without logging in:

```bash
aef-grits-auth status
```

Explicitly log in and then verify:

```bash
aef-grits-auth login \
  --source earthengine \
  --auth-mode localhost \
  --project YOUR_GEE_PROJECT

aef-grits-auth verify --source auto --project YOUR_GEE_PROJECT
```

For a remote shell, select `--auth-mode gcloud` or `notebook` deliberately.
ADC is also explicit:

```bash
aef-grits-auth login --source adc --project YOUR_GEE_PROJECT
aef-grits-auth verify --source adc --project YOUR_GEE_PROJECT
```

## Recommended modes

| Environment | Recommended identity |
|---|---|
| Personal Windows/macOS/Linux | Earth Engine persistent user credential; `auto` for downloads |
| Single-user SSH server | Explicit `gcloud` or `notebook` login before starting a job |
| Shared campus Web server | Per-user Google OAuth; no server-wide Project fallback |
| Institution-managed unattended server | Optional service-account ADC configured by an administrator |
| Google Cloud runtime | Attached least-privilege service account through ADC |
| CI | Workload Identity Federation; never commit a long-lived key |

The server-wide ADC mode is optional. It is not used to impersonate Web users.

## Shared Web OAuth deployment

The local default remains `AEF_GRITS_WEB_AUTH_MODE=local`. For a shared server,
place all state and the encrypted token vault on ext4/XFS, terminate TLS at a
trusted reverse proxy, and configure runtime secrets outside Git:

```bash
aef-grits-auth vault-init \
  --database /var/lib/aef-grits/auth/vault.sqlite \
  --key-file /etc/aef-grits/vault.key

export AEF_GRITS_WEB_AUTH_MODE=oauth
export AEF_GRITS_WEB_OAUTH_CLIENT=/etc/aef-grits/oauth-client.json
export AEF_GRITS_WEB_OAUTH_REDIRECT_URI=https://aef.example.edu/api/auth/callback
export AEF_GRITS_AUTH_VAULT=/var/lib/aef-grits/auth/vault.sqlite
export AEF_GRITS_AUTH_VAULT_KEY=/etc/aef-grits/vault.key
export AEF_GRITS_WEB_ALLOWED_DOMAINS=example.edu
export AEF_GRITS_WEB_TRUSTED_HOSTS=aef.example.edu
export AEF_GRITS_WEB_PROXY_COUNT=1
```

Do not set a global `AEF_GRITS_PROJECT` for this mode. After login, every user
enters a Project they can access. The server verifies it before signing the
plan. Plans and tasks are bound to an anonymous owner hash, credential version,
and Project fingerprint; another user cannot view, cancel, or resume them.

Refresh tokens are encrypted at rest and never placed in command arguments,
logs, Zarr stores, Parquet files, or NTFS. A child downloader receives only an
opaque handle through its private process environment. If authorization is
revoked or expires, the process stops at its checkpoint with `auth_required`.
The user logs in again and explicitly resumes the task.

Direct `computeFeatures` and `computePixels` downloads do not request Google
Drive access. Legacy Drive export/download tools remain a separate, opt-in
credential path.

## Operational states

The UI exposes `not connected`, `connected/project required`, `ready`, and
`reauthentication required`. Authentication and network failures are not
mixed: 401/403 credential or Project failures pause work, while retryable TLS,
timeout, 429, and 5xx failures use the bounded network retry policy.

Never upload credential JSON through the browser. OAuth client secrets, vault
keys, session secrets, and service-account material belong in an OS secret
store, systemd credentials, or an external secret manager—not in Git or the
download output directory.
