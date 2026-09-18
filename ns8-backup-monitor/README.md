# ns8-backup-monitor

A [NethServer 8](https://github.com/NethServer/ns8-core) module that watches
an IMAP mailbox for backup report emails (originally built for 1Backup /
CoreTech-style reports), parses them automatically, and shows each report's
**severity** and **destination**.

Built starting from the [ns8-kickstart](https://github.com/NethServer/ns8-kickstart)
module template.

## Two separate surfaces

- **NS8 admin panel** (Settings only, inside the NS8 cluster admin UI):
  for the sysadmin only. Configure the IMAP mailbox(es) to watch, the
  public FQDN, and the history retention period. It also has a read-only
  Reports view. It has **no** access to dashboard user management.
- **Standalone public dashboard**, reachable directly at its own FQDN
  (e.g. `https://backup-monitor.example.org`), with its own login,
  **password + MFA (TOTP)**, and **multiple independent user accounts**
  managed entirely from within the dashboard itself (see "User
  management" below) — no NS8 action, no NS8 admin access needed for any
  of it. This is a read-only view of the reports — no IMAP settings here.

## How it works

- A single container runs two things at once (`app/main.py`):
  1. an **IMAP poller** that logs into the configured mailbox on a
     schedule and looks for messages from one specific sender address;
  2. a small **Flask web app** serving the login page and the dashboard.
- Each matching email is parsed (`app/parsing.py`): the report's HTML body
  and any PDF attachment are read, and fields such as user, backup set,
  **destination**, data size, and job status are extracted. A single email
  can describe more than one destination attempt for the same job (e.g. a
  primary and a fallback destination) — these become separate records.
- The job status text is used to classify a **severity**:
  - `CRITICAL` — e.g. "failed", "error", disk full, timeout, authentication
  - `WARNING` — e.g. "skipped", "still running", quota-related, partial
  - `OK` — completed successfully
  - `INFO` — anything else / not recognized
- Parsed records are stored in a local SQLite database
  (`/state/reports.db`, on a volume shared between the container and the
  NS8 action scripts). Reports older than the configured retention period
  (default **15 days**) are deleted automatically after each poll cycle.
- **User accounts** for the dashboard (username, password, TOTP secret,
  an `is_admin` flag) live in the same database, but are managed
  **entirely within the web app itself** — there is no NS8 action and no
  self-service open registration:
  - The very first visit to the dashboard, with zero users in the
    database, is redirected to a one-time `/setup` flow that creates the
    first account (always an admin), followed by a mandatory MFA
    confirmation step before the very first login.
  - From then on, any admin account can add/list/delete further accounts
    from `/admin/users` (linked as "Utenti" in the dashboard header for
    admins only). Regular accounts only see the reports dashboard.
  - Safeguards: a username can't be reused, an admin can't delete their
    own account, and the very last remaining admin account can't be
    deleted (to avoid locking everyone out).
- Password hashing (PBKDF2-HMAC-SHA256) and TOTP (RFC 6238, verified
  against the reference `pyotp` library during development) are
  implemented with the Python standard library only, in `app/authlib.py`.
- Login attempts are throttled: 5 failed attempts lock the account for 15
  minutes.

## Known limitation

The destination name can appear slightly differently between the report's
HTML email body (e.g. `DC2-1Backup`) and its PDF attachment (e.g.
`DC2-1Backup (Predefined Destination)`) for the same job — this is how the
source reports themselves are written, not a parsing bug. When both the
HTML body and a PDF attachment are present, the HTML body's version is
used and the PDF's matching record is skipped only if its destination
string is an exact match; otherwise both may appear. Adjust the
deduplication logic in `parse_email_message()` (`app/parsing.py`) if your
own reports need stricter matching.

## Repository layout

```
app/                    Combined poller + web app container source
  main.py               Entrypoint: starts the poller in a background
                         thread, serves the Flask app in the foreground
  poller.py             IMAP polling loop, parsing, retention cleanup
  webapp.py             Flask app: setup/login (password + TOTP), user
                         management (admin-only), dashboard,
                         /api/reports (session-authenticated, JSON)
  parsing.py            Report parsing / severity classification
  authlib.py            Password hashing + TOTP (stdlib only)
  db.py                 Shared SQLite schema (reports, users, ...)
  templates/            login.html, setup.html, setup_verify.html,
                         admin_users.html, dashboard.html
  Containerfile
  requirements.txt

imageroot/
  actions/
    configure-module/   Save IMAP settings + FQDN + retention, set up the
                         Traefik route (dedicated virtual host, TLS via
                         Let's Encrypt), (re)start the service
    get-configuration/  Return the saved settings (password masked)
    list-reports/       Read reports.db (used by the NS8 admin UI's own
                         read-only Reports view, separate from the public
                         dashboard's own /api/reports)
    destroy-module/     Remove the Traefik route
  systemd/user/
    backup-monitor.service   Runs the poller+web container via Podman,
                              publishing the TCP port Traefik proxies to

ui/                     NS8 admin UI (Vue 2 + Carbon + @nethserver/ns8-ui-lib)
  src/views/Settings.vue  IMAP + FQDN + retention configuration form
  src/views/Reports.vue   Severity/destination view, inside the NS8 admin UI

build-images.sh         Builds both the app image and the module image
```

## Install

    add-module ghcr.io/nethserver/backup-monitor:latest 1

Output example:

    {"module_id": "backup-monitor1", "image_name": "backup-monitor", "image_url": "ghcr.io/nethserver/backup-monitor:latest"}

## Configure

From the NS8 admin UI (module → Settings), or via `api-cli`:

    api-cli run module/backup-monitor1/configure-module --data '{
        "imap_host": "mail.example.org",
        "imap_username": "support@example.org",
        "imap_password": "secret",
        "imap_folders": ["INBOX", "Backup Reports"],
        "sender_filters": ["backup@1backup.it", "noreply@veeam.example"],
        "fqdn": "backup-monitor.example.org"
    }'

Fields:

- `imap_host` (required), `imap_port` (default `993`), `imap_ssl` (default `true`)
- `imap_username` (required), `imap_password` (required the first time;
  omit on later calls to keep it unchanged)
- `imap_folders`: array of folders to check, default `["INBOX"]` — you can
  watch more than one folder (e.g. `["INBOX", "Backup Reports"]`)
- `sender_filters` (required): array of sender addresses to analyze — you
  can monitor reports coming from more than one backup product/sender at
  once (e.g. `["backup@1backup.it", "noreply@veeam.example"]`)
- `poll_interval`: seconds between IMAP checks, default `300`
- `retention_days`: how many days of report history to keep, default `15`
- `fqdn` (required): the public domain the dashboard will be reachable
  at. **Must already resolve to this NS8 node** before you configure the
  module, since a Let's Encrypt certificate is requested for it
  immediately.

## Create the first dashboard user

Once the module is configured and its FQDN resolves, open
`https://<fqdn>/` in a browser: with no user accounts yet, you'll land on
a one-time setup page to create the first account (always an admin),
followed by a mandatory TOTP confirmation step. From then on, that admin
can add further accounts from the "Utenti" page in the dashboard header —
no NS8 CLI or admin access is needed for any of this.

## Uninstall

    remove-module --no-preserve backup-monitor1

## Development notes

- `build-images.sh` builds **two** images: the app (`app/Containerfile`,
  running both the poller and the web dashboard) and the module's own
  metadata image (built `FROM scratch`, carrying only `imageroot/` and the
  compiled `ui/dist`, labelled with `org.nethserver.images` pointing at
  the app image — **the label must include an explicit `:latest` tag**,
  or NS8 core's `create-module` step fails with a Python `ValueError`
  while trying to split the image reference). `clean-registry.yml`
  already lists both image names.
- The UI was built and verified locally with `yarn install && yarn build`
  (Node 24) against the pinned `@nethserver/ns8-ui-lib` / `@carbon/vue`
  versions in `ui/package.json`.
- Multiple folders and multiple sender addresses are supported: the
  poller iterates every configured folder and searches each one with a
  nested IMAP `OR FROM ... FROM ...` criteria matching any sender.
  Deduplication of already-processed messages uses a composite
  `folder::uidvalidity::uid` key, since UIDVALIDITY is only unique within
  a single mailbox folder, not across folders.
- `authlib.py`'s TOTP implementation was cross-checked against the
  reference `pyotp` library and produces byte-identical codes; the full
  login flow (correct/incorrect password, correct/incorrect TOTP,
  lockout after 5 failed attempts, logout), the first-run `/setup` +
  MFA-confirmation flow, and the admin-only `/admin/users`
  create/list/delete flow (including duplicate-username, self-delete and
  last-admin protections) were all exercised end-to-end with Flask's
  test client.
- Labels on the module image (`org.nethserver.rootfull`,
  `org.nethserver.tcp-ports-demand=1`,
  `org.nethserver.authorizations=traefik@node:routeadm`) and the
  `set-route`/`delete-route` calls to `traefik@node` (using `host` for a
  dedicated virtual host rather than `path`) were set by analogy with
  `ns8-kickstart` and other NS8 modules; verify them against a real NS8
  dev cluster before a production release — see the
  [module development docs](https://nethserver.github.io/ns8-core/modules/new_module).

## Running tests locally

This module uses the NS8 standard testing infrastructure. See
[Running tests locally](https://github.com/NethServer/ns8-github-actions/blob/v1/README.md#running-tests-locally)
in the ns8-github-actions README. `tests/backup-monitor.robot` currently
still contains the template's placeholder test and needs to be adapted to
exercise `configure-module` and `list-reports` (dashboard user management
lives entirely in the web app now, outside of NS8 actions, and would need
its own kind of test — e.g. a simple HTTP-level check against `/setup`).

## UI translation

Translated with [Weblate](https://hosted.weblate.org/projects/ns8/). Only
the `en` and `it` locale files were updated for this module; other
languages in `ui/public/i18n/` still contain the ns8-kickstart template's
generic strings and should be re-translated (via Weblate, once this
repository is registered there, or by hand).
