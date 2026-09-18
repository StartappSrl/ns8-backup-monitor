# ns8-backup-monitor

A [NethServer 8](https://github.com/NethServer/ns8-core) module that watches
an IMAP mailbox for backup report emails (originally built for 1Backup /
CoreTech-style reports), parses them automatically, and shows each report's
**severity** and **destination** in a dashboard.

Built starting from the [ns8-kickstart](https://github.com/NethServer/ns8-kickstart)
module template.

## How it works

- A small **poller service** (`app/poller.py`), running in its own
  container, logs into the configured IMAP mailbox on a schedule and looks
  for messages from one specific sender address.
- Each matching message is parsed (`app/parsing.py`): the report's HTML body
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
  (`/state/reports.db`, on a volume shared with the poller container).
- The NS8 admin UI reads that database through the `list-reports` action
  and shows it as a filterable dashboard (severity summary, filter by
  severity/destination, search by user or backup set, detail view per
  report).

No public HTTP endpoint is exposed: the UI talks to the module exclusively
through NS8 actions (`configure-module`, `get-configuration`,
`list-reports`), the same mechanism used for every other NS8 module
setting. The poller container only needs outbound access to your IMAP
server.

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
app/                    Poller container source (not part of the module's
                         own "scratch" image — see build-images.sh)
  poller.py             IMAP polling loop + SQLite storage
  parsing.py            Report parsing / severity classification
  Containerfile         Image definition for the poller
  requirements.txt

imageroot/
  actions/
    configure-module/   Save IMAP settings, (re)start the poller service
    get-configuration/  Return the saved settings (password masked)
    list-reports/        Read reports.db, return filtered records + summary
    destroy-module/      No-op (module has no external route to clean up)
  systemd/user/
    backup-monitor.service   Runs the poller container via Podman

ui/                     NS8 admin UI (Vue 2 + Carbon + @nethserver/ns8-ui-lib)
  src/views/Settings.vue  IMAP configuration form
  src/views/Reports.vue   Severity/destination dashboard

build-images.sh         Builds both the poller image and the module image
```

## Install

Instantiate the module with:

    add-module ghcr.io/nethserver/backup-monitor:latest 1

Output example:

    {"module_id": "backup-monitor1", "image_name": "backup-monitor", "image_url": "ghcr.io/nethserver/backup-monitor:latest"}

## Configure

Launch `configure-module` on the instance (`backup-monitor1` in this
example), setting:

- `imap_host` (required): IMAP server hostname
- `imap_port`: default `993`
- `imap_ssl`: default `true`
- `imap_username` (required)
- `imap_password` (required the first time; omit on later calls to keep it unchanged)
- `imap_folder`: default `INBOX`
- `sender_filter` (required): only emails from this address are analyzed,
  e.g. `backup@1backup.it`
- `poll_interval`: seconds between IMAP checks, default `300`

Example:

    api-cli run module/backup-monitor1/configure-module --data '{
        "imap_host": "mail.example.org",
        "imap_username": "support@example.org",
        "imap_password": "secret",
        "sender_filter": "backup@1backup.it"
    }'

This writes the settings to the instance's `state/config.env` (readable
only by the module's own user) and (re)starts the poller service.

To see stored reports directly from the CLI:

    api-cli run module/backup-monitor1/list-reports --data '{}'

## Uninstall

    remove-module --no-preserve backup-monitor1

## Development notes

- `build-images.sh` builds **two** images: the poller (`app/Containerfile`,
  a normal standalone image) and the module's own metadata image (built
  `FROM scratch`, carrying only `imageroot/` and the compiled `ui/dist`,
  labelled with `org.nethserver.images` pointing at the poller image).
  `clean-registry.yml` already lists both image names.
- The UI was built and verified locally with `yarn install && yarn build`
  (Node 24) against the pinned `@nethserver/ns8-ui-lib` / `@carbon/vue`
  versions in `ui/package.json`.
- Labels on the module image (`org.nethserver.rootfull`,
  `org.nethserver.tcp-ports-demand=0`, no `authorizations` label since no
  Traefik route is needed) were set by analogy with `ns8-kickstart`; verify
  them against a real NS8 dev cluster before a production release — see
  the [module development docs](https://nethserver.github.io/ns8-core/modules/new_module).

## Running tests locally

This module uses the NS8 standard testing infrastructure. See
[Running tests locally](https://github.com/NethServer/ns8-github-actions/blob/v1/README.md#running-tests-locally)
in the ns8-github-actions README. `tests/backup-monitor.robot` currently
still contains the template's placeholder test and needs to be adapted to
exercise `configure-module` and `list-reports`.

## UI translation

Translated with [Weblate](https://hosted.weblate.org/projects/ns8/). Only
the `en` and `it` locale files were updated for this module; other
languages in `ui/public/i18n/` still contain the ns8-kickstart template's
generic strings and should be re-translated (via Weblate, once this
repository is registered there, or by hand).
