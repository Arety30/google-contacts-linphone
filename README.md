# Google Contacts to Linphone (CardDAV) importers

Three small Python scripts that take a person's Google Contacts and load them into a CardDAV address book that the Linphone softphone syncs from. Linphone can only show caller names and offer a dialable contact list if the contacts are in its CardDAV store, and Google does not speak CardDAV to third-party clients, so these scripts do the conversion: they read contacts from the Google People API (`sync.py`), from a Google Takeout export (`import_takeout.py`) or from the phone contacts cached by KDE Connect (`import_kde.py`), normalise the phone numbers, emit one clean vCard per contact with a stable UID, and `PUT` each card into a Radicale CardDAV server.

## Where it is used

In Abrar's setup Radicale runs on the laptop behind a reverse proxy and Linphone (configured by the companion `twilio-sms-bridge` project) uses it as its remote address book. A one-off Takeout import seeded the book, and the KDE Connect importer adds anything that exists only on the phone. Because UIDs are deterministic, the scripts can be re-run at any time and simply overwrite the existing cards.

## How it works

### Shared logic (`sync.py`)

- **Configuration.** `RADICALE_URL` (default `http://127.0.0.1:5232/USER/contacts/`), `RADICALE_USER` (the value sent in the `X-Remote-User` header, for a Radicale configured with `auth type = http_x_remote_user` behind a trusted proxy) and `DEFAULT_COUNTRY_CODE` (default `+974`) are read from the environment.
- **Phone normalisation (`norm_phone`).** Strips everything except digits and `+`; discards values longer than 20 characters (garbage such as leaked base64); converts a leading `00` to `+`; keeps `+` numbers if at least 8 characters long; keeps 5-digit-or-shorter short codes as dialled; prefixes 8-digit local numbers with `DEFAULT_COUNTRY_CODE`; adds `+` to 11-digit numbers starting with the default country's digits; anything else is passed through unchanged.
- **vCard emission.** `person_to_vcard` builds a vCard 3.0 with `UID:google-<sha1(resourceName)[:24]>`, `FN`, `N`, optional `NICKNAME`, de-duplicated `TEL;TYPE=CELL|HOME|WORK|VOICE` lines, `EMAIL;TYPE=HOME|WORK`, the first `ORG`, and an `X-GOOGLE-RESOURCE` back-reference. Contacts with neither phone nor email are skipped; a missing display name falls back to nickname, organisation, first phone or first email.
- **Upload (`push_to_radicale`).** One HTTP `PUT` per card to `<RADICALE_URL><uid>.vcf` with `Content-Type: text/vcard`. With `prune=True` it then does a `PROPFIND Depth: 1`, and deletes every `google-*.vcf` that is not in the current set, leaving cards from other sources alone.
- **Commands.** `login` runs the installed-app OAuth flow (scope `contacts.readonly`, local redirect) and saves the token with mode 600; `pull` pages through `people.connections.list` (1000 per page) and caches the raw JSON; `push` = pull + convert + upload (`--prune` to remove stale Google cards); `status` reports whether the client secret and token exist and how many `.vcf` entries Radicale holds.

### Takeout importer (`import_takeout.py`)

- Accepts a Takeout `.zip`, an extracted directory, a `.vcf` or a `.csv`. Prefers vCard files; if only CSV is present, `csv_to_vcf` turns Google's `All Contacts.csv` columns (name parts, nickname, organisation, `Phone N - Value/Label`, `E-mail N - Value/Label`, `:::`-joined duplicates) into vCard 3.0 text so a single parser serves both.
- `unfold`/`parse_cards`/`prop` implement a minimal vCard reader (line unfolding, `BEGIN`/`END` splitting, parameter parsing).
- `convert` produces a **vCard 4.0** (the comment notes Linphone's belcard parser requests 4.0), with UID `google-<sha1 of UID / X-GOOGLE-ID / X-ABUID, or of name+numbers+emails>`. For every `+` number it also adds `IMPP:sip:<number>@<SIP_DOMAIN>` so that Linphone can dial the contact directly and match the name on incoming calls whose From is `sip:+E164@domain`. `SIP_DOMAIN` comes from the environment or from the sibling project's `linphone.env`.
- Flags: `--dry-run` prints the first five cards instead of uploading; `--prune` as above.

### KDE Connect importer (`import_kde.py`)

- Reads the `.vcf` files KDE Connect caches under `~/.local/share/kpeoplevcard/kdeconnect-*/`. These are vCard 2.1 with `QUOTED-PRINTABLE` encoding; `decode_qp` joins soft line breaks, decodes the values and drops `PHOTO` blocks.
- Unless `--all` is given, it first collects every `TEL` already present in Radicale from non-`kde-` cards and skips contacts whose numbers are all already there, so phone-only contacts are added without duplicating the Google ones.
- UIDs are `kde-<sha1(X-KDECONNECT-ID)>`; uploads never prune.

### Files

| File | Role |
|---|---|
| `sync.py` | People API sign-in/pull/push, normalisation helpers, Radicale upload |
| `import_takeout.py` | Takeout zip/dir/vcf/csv -> vCard 4.0 with SIP addresses -> Radicale |
| `import_kde.py` | KDE Connect vCard 2.1 cache -> Radicale, de-duplicated against existing numbers |
| `run` | `cd` to the project and run `sync.py` with the project's `.venv` |

Storage: `~/.config/google-contacts-linphone/{client_secret.json, token.json, contacts.json}`; the address book itself lives in Radicale.

## Setup

Requirements: Python 3.10+, a reachable Radicale (or other CardDAV server that accepts `PUT`/`PROPFIND`/`DELETE` with the `X-Remote-User` header), and for `sync.py` a Google Cloud project with the People API enabled and an OAuth client of type "Desktop app".

```bash
cd ~/Workspace/google-contacts-linphone
python3 -m venv .venv
.venv/bin/pip install requests google-auth google-auth-oauthlib google-api-python-client

mkdir -p ~/.config/google-contacts-linphone
cp ~/Downloads/client_secret_*.json ~/.config/google-contacts-linphone/client_secret.json
```

Environment variables (all optional):

```bash
export RADICALE_URL=http://127.0.0.1:5232/USER/contacts/
export RADICALE_USER=USER
export DEFAULT_COUNTRY_CODE=+10
export SIP_DOMAIN=example.sip.twilio.com     # for IMPP lines in import_takeout.py
```

## Usage

```bash
./run login                     # browser sign-in, token saved locally
./run pull                      # download and summarise
./run push                      # upload to Radicale
./run push --prune              # also delete google-* cards no longer in Google
./run status

# Takeout export
.venv/bin/python import_takeout.py ~/Downloads/takeout-contacts.zip --dry-run
.venv/bin/python import_takeout.py ~/Downloads/takeout-contacts.zip --prune

# phone contacts via KDE Connect
.venv/bin/python import_kde.py --dry-run
.venv/bin/python import_kde.py
```

Afterwards, in Linphone open Settings > Contacts > CardDAV and refresh the remote address book.

## Project structure

```
google-contacts-linphone/
├── run                  # wrapper: .venv/bin/python sync.py "$@"
├── sync.py
├── import_takeout.py
└── import_kde.py
```

## Limitations and notes

- One-way only: edits made in Linphone or Radicale are never written back to Google.
- `sync.py` emits vCard 3.0 while `import_takeout.py` emits 4.0 and adds `IMPP` SIP lines; if Linphone refuses 3.0 cards, use the Takeout path or adjust `person_to_vcard`.
- Photos, addresses, birthdays and notes are deliberately dropped; only names, numbers, emails and organisation are kept.
- Phone normalisation assumes a country with 8-digit local numbers by default; set `DEFAULT_COUNTRY_CODE` (and review `norm_phone`) for other regions.
- The Radicale auth model relies on the server trusting `X-Remote-User`; do not expose that port directly.
- `cmd_login` prints the signed-in account's email to the terminal only.
