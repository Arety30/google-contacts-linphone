#!/usr/bin/env python3
"""Sign in with Google, pull your Google Contacts, and load them into the
Linphone CardDAV address book (Radicale on this laptop).

Commands:
  login   open the browser, sign in with Google, store the token locally
  pull    download contacts from the People API and show a summary
  push    pull + convert to vCard 3.0 + upload into Radicale (what Linphone syncs)
  status  show token / address-book state

Files (all private to this laptop):
  ~/.config/google-contacts-linphone/client_secret.json  OAuth "Desktop app" client from Google Cloud Console
  ~/.config/google-contacts-linphone/token.json          your sign-in token (created by `login`)
  ~/.config/google-contacts-linphone/contacts.json       last pulled contacts (created by `pull`/`push`)
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

CONF = Path.home() / ".config" / "google-contacts-linphone"
CLIENT_SECRET = CONF / "client_secret.json"
TOKEN = CONF / "token.json"
CACHE = CONF / "contacts.json"

SCOPES = ["https://www.googleapis.com/auth/contacts.readonly"]
PERSON_FIELDS = "names,phoneNumbers,emailAddresses,organizations,nicknames"

# Radicale listens on 127.0.0.1:5232 with auth type http_x_remote_user; the
# public URL https://example.com/dav/abrar/contacts/ is Apache in front of it.
RADICALE = os.environ.get("RADICALE_URL", "http://127.0.0.1:5232/abrar/contacts/")
RADICALE_USER = os.environ.get("RADICALE_USER", "abrar")
DEFAULT_CC = os.environ.get("DEFAULT_COUNTRY_CODE", "+974")  # Qatar


# ----------------------------------------------------------------------------- auth
def credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    if creds and creds.valid:
        return creds
    if not interactive:
        sys.exit("Not signed in. Run:  ./run login")
    if not CLIENT_SECRET.exists():
        sys.exit(
            f"Missing {CLIENT_SECRET}\n"
            "Create an OAuth client (type: Desktop app) in Google Cloud Console, enable the\n"
            "People API, download the JSON and save it at that path. See README.md."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    # Opens the default browser on this laptop; Google redirects back to localhost.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    return creds


# ----------------------------------------------------------------------------- people api
def fetch_contacts(creds):
    from googleapiclient.discovery import build

    svc = build("people", "v1", credentials=creds, cache_discovery=False)
    people, token = [], None
    while True:
        resp = (
            svc.people()
            .connections()
            .list(
                resourceName="people/me",
                pageSize=1000,
                personFields=PERSON_FIELDS,
                pageToken=token,
            )
            .execute()
        )
        people.extend(resp.get("connections", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return people


# ----------------------------------------------------------------------------- normalisation
def norm_phone(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"[^\d+]", "", s)
    if len(digits) > 20 or not digits:  # garbage (e.g. base64 that leaked into a TEL line)
        return None
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        return digits if len(digits) >= 8 else None
    if len(digits) <= 5:  # short codes (191, 999, 97700 …) – keep as dialled
        return digits
    if len(digits) == 8:  # Qatar local mobile/landline
        return DEFAULT_CC + digits
    if digits.startswith("974") and len(digits) == 11:
        return "+" + digits
    return digits  # unknown shape: keep as-is, Linphone will dial it literally


def esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def person_to_vcard(p: dict) -> tuple[str, str] | None:
    rn = p["resourceName"]  # people/c123…
    uid = "google-" + hashlib.sha1(rn.encode()).hexdigest()[:24]

    names = p.get("names") or []
    n = names[0] if names else {}
    fn = (n.get("displayName") or "").strip()
    given = n.get("givenName", "")
    family = n.get("familyName", "")

    phones = []
    for ph in p.get("phoneNumbers") or []:
        num = norm_phone(ph.get("canonicalForm") or ph.get("value") or "")
        if num and num not in [x[0] for x in phones]:
            phones.append((num, (ph.get("type") or "cell").lower()))
    emails = [(e.get("value"), (e.get("type") or "other").lower()) for e in p.get("emailAddresses") or [] if e.get("value")]
    orgs = [o.get("name") for o in p.get("organizations") or [] if o.get("name")]
    nick = ((p.get("nicknames") or [{}])[0]).get("value")

    if not phones and not emails:
        return None  # nothing Linphone could use
    if not fn:
        fn = nick or orgs[0] if (nick or orgs) else (phones[0][0] if phones else emails[0][0])

    tel_type = {"mobile": "CELL", "cell": "CELL", "home": "HOME", "work": "WORK", "main": "VOICE"}
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"UID:{uid}",
        f"FN:{esc(fn)}",
        f"N:{esc(family)};{esc(given)};;;",
    ]
    if nick:
        lines.append(f"NICKNAME:{esc(nick)}")
    for num, t in phones:
        lines.append(f"TEL;TYPE={tel_type.get(t, 'VOICE')}:{num}")
    for addr, t in emails:
        lines.append(f"EMAIL;TYPE={'WORK' if t == 'work' else 'HOME'}:{addr}")
    if orgs:
        lines.append(f"ORG:{esc(orgs[0])}")
    lines.append(f"X-GOOGLE-RESOURCE:{rn}")
    lines.append("END:VCARD")
    return uid, "\r\n".join(lines) + "\r\n"


# ----------------------------------------------------------------------------- carddav
def push_to_radicale(cards: dict[str, str], prune: bool) -> tuple[int, int]:
    import requests

    hdr = {"X-Remote-User": RADICALE_USER}
    put_hdr = {**hdr, "Content-Type": "text/vcard; charset=utf-8"}
    ok = 0
    for uid, vcf in cards.items():
        r = requests.put(RADICALE + uid + ".vcf", data=vcf.encode("utf-8"), headers=put_hdr, timeout=30)
        if r.status_code in (200, 201, 204):
            ok += 1
        else:
            print(f"  ! {uid}: HTTP {r.status_code} {r.text[:120]}")
    removed = 0
    if prune:
        r = requests.request("PROPFIND", RADICALE, headers={**hdr, "Depth": "1"}, timeout=30)
        for href in re.findall(r"<href>([^<]+\.vcf)</href>", r.text):
            name = href.rsplit("/", 1)[-1]
            if name.startswith("google-") and name[:-4] not in cards:
                requests.delete(RADICALE + name, headers=hdr, timeout=30)
                removed += 1
    return ok, removed


# ----------------------------------------------------------------------------- commands
def cmd_login():
    creds = credentials(interactive=True)
    import requests

    me = requests.get(
        "https://people.googleapis.com/v1/people/me?personFields=names,emailAddresses",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    ).json()
    who = (me.get("emailAddresses") or [{}])[0].get("value", "?")
    print(f"Signed in as {who}. Token saved to {TOKEN}")


def cmd_pull():
    creds = credentials(interactive=False)
    people = fetch_contacts(creds)
    CACHE.write_text(json.dumps(people, indent=1))
    CACHE.chmod(0o600)
    cards = {}
    for p in people:
        r = person_to_vcard(p)
        if r:
            cards[r[0]] = r[1]
    print(f"Google contacts: {len(people)}   usable (phone or email): {len(cards)}")
    return cards


def cmd_push(prune: bool):
    cards = cmd_pull()
    ok, removed = push_to_radicale(cards, prune)
    print(f"Uploaded to Linphone address book: {ok}" + (f"   removed stale: {removed}" if prune else ""))
    print("Linphone: Settings → Contacts → CardDAV → refresh / re-sync to see them.")


def cmd_status():
    import requests

    print("client secret:", "present" if CLIENT_SECRET.exists() else "MISSING")
    print("token:        ", "present" if TOKEN.exists() else "not signed in")
    r = requests.request("PROPFIND", RADICALE, headers={"X-Remote-User": RADICALE_USER, "Depth": "1"}, timeout=30)
    n = len(re.findall(r"\.vcf</href>", r.text))
    print(f"Radicale {RADICALE}: HTTP {r.status_code}, {n} contacts")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "login":
        cmd_login()
    elif cmd == "pull":
        cmd_pull()
    elif cmd == "push":
        cmd_push(prune="--prune" in sys.argv)
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)
