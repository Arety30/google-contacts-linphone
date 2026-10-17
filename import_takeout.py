#!/usr/bin/env python3
"""Import a Google Takeout *Contacts* export into the Linphone CardDAV address book.

Usage:
  import_takeout.py <takeout.zip | extracted Takeout dir | All Contacts.vcf> [--prune] [--dry-run]

Takeout ships contacts as vCard 3.0 (Contacts/All Contacts/All Contacts.vcf) and
optionally CSV. We read the vCard, re-emit a clean vCard 3.0 per contact with a
stable UID, normalise phone numbers (8-digit Qatar numbers get +974) and PUT each
one into Radicale, which is what Linphone syncs from.
"""
import hashlib
import io
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync import RADICALE, esc, norm_phone, push_to_radicale  # noqa: E402


def _sip_domain() -> str:
    if os.environ.get("SIP_DOMAIN"):
        return os.environ["SIP_DOMAIN"]
    env = Path.home() / "Workspace/twilio/linphone/linphone.env"
    if env.exists():
        m = re.search(r"^SIP_DOMAIN=(\S+)", env.read_text(), re.M)
        if m:
            return m.group(1).strip().strip('"')
    return ""


SIP_DOMAIN = _sip_domain()


def csv_to_vcf(text: str) -> str:
    """Google Takeout 'All Contacts.csv' -> vCard 3.0 text (so one parser serves both)."""
    import csv

    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        g = lambda k: (r.get(k) or "").strip()  # noqa: E731
        first, middle, last = g("First Name"), g("Middle Name"), g("Last Name")
        fn = " ".join(x for x in (g("Name Prefix"), first, middle, last, g("Name Suffix")) if x)
        lines = ["BEGIN:VCARD", "VERSION:3.0"]
        if fn:
            lines.append("FN:" + esc(fn))
            lines.append(f"N:{esc(last)};{esc(first)};{esc(middle)};{esc(g('Name Prefix'))};{esc(g('Name Suffix'))}")
        if g("Nickname"):
            lines.append("NICKNAME:" + esc(g("Nickname")))
        if g("Organization Name"):
            lines.append("ORG:" + esc(g("Organization Name")))
        for i in range(1, 10):
            v = g(f"Phone {i} - Value")
            if not v:
                continue
            label = g(f"Phone {i} - Label").upper()
            typ = "WORK" if "WORK" in label else "HOME" if "HOME" in label else "CELL"
            for num in v.split(":::"):  # Takeout joins duplicates with ' ::: '
                if num.strip():
                    lines.append(f"TEL;TYPE={typ}:{num.strip()}")
        for i in range(1, 10):
            v = g(f"E-mail {i} - Value")
            if not v:
                continue
            typ = "WORK" if "WORK" in g(f"E-mail {i} - Label").upper() else "HOME"
            for addr in v.split(":::"):
                if addr.strip():
                    lines.append(f"EMAIL;TYPE={typ}:{addr.strip()}")
        lines.append("END:VCARD")
        out.append("\r\n".join(lines))
    return "\r\n".join(out) + "\r\n"


def _pick_csv(names: list[str]) -> list[str]:
    allc = [n for n in names if n.lower().endswith("all contacts.csv")]
    return allc or [n for n in names if n.lower().endswith(".csv")]


def load_vcf_text(src: Path) -> str:
    if src.is_file() and src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".vcf")]
            if names:
                return "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
            csvs = _pick_csv(z.namelist())
            if not csvs:
                sys.exit(f"No .vcf or .csv inside {src}")
            return "\n".join(csv_to_vcf(z.read(n).decode("utf-8", "replace")) for n in csvs)
    if src.is_dir():
        files = sorted(src.rglob("*.vcf"))
        if files:
            return "\n".join(f.read_text("utf-8", "replace") for f in files)
        csvs = _pick_csv([str(p) for p in src.rglob("*.csv")])
        if not csvs:
            sys.exit(f"No .vcf or .csv under {src}")
        return "\n".join(csv_to_vcf(Path(n).read_text("utf-8", "replace")) for n in csvs)
    text = src.read_text("utf-8", "replace")
    return csv_to_vcf(text) if src.suffix.lower() == ".csv" else text


def unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_cards(text: str):
    card, cards = None, []
    for line in unfold(text):
        u = line.upper()
        if u.startswith("BEGIN:VCARD"):
            card = []
        elif u.startswith("END:VCARD"):
            if card is not None:
                cards.append(card)
            card = None
        elif card is not None and ":" in line:
            card.append(line)
    return cards


def unesc(v: str) -> str:
    return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").replace("\\\\", "\\").strip()


def prop(line: str):
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), [p.upper() for p in params], value


def convert(card: list[str]) -> tuple[str, str] | None:
    fn = n = nick = org = None
    tels: list[tuple[str, str]] = []
    emails: list[tuple[str, str]] = []
    src_id = None
    for line in card:
        name, params, value = prop(line)
        if name == "FN":
            fn = unesc(value)
        elif name == "N":
            n = value
        elif name == "NICKNAME":
            nick = unesc(value)
        elif name == "ORG":
            org = unesc(value.split(";")[0])
        elif name == "TEL":
            num = norm_phone(value)
            if num and num not in [t[0] for t in tels]:
                t = "WORK" if any("WORK" in p for p in params) else "HOME" if any("HOME" in p for p in params) else "CELL"
                tels.append((num, t))
        elif name == "EMAIL":
            addr = unesc(value)
            if addr and addr not in [e[0] for e in emails]:
                emails.append((addr, "WORK" if any("WORK" in p for p in params) else "HOME"))
        elif name in ("UID", "X-GOOGLE-ID", "X-ABUID"):
            src_id = src_id or value
    if not tels and not emails:
        return None
    if not fn:
        fn = nick or org or (tels[0][0] if tels else emails[0][0])
    key = src_id or (fn + "|" + "|".join(sorted(t for t, _ in tels)) + "|".join(sorted(e for e, _ in emails)))
    uid = "google-" + hashlib.sha1(key.encode()).hexdigest()[:24]
    # vCard 4.0: Linphone (belcard) requests version 4.0 and refuses to parse 3.0.
    lines = ["BEGIN:VCARD", "VERSION:4.0", f"UID:{uid}", f"FN:{esc(fn)}"]
    lines.append("N:" + n if n else f"N:;{esc(fn)};;;")
    if nick:
        lines.append(f"NICKNAME:{esc(nick)}")
    for num, t in tels:
        lines.append(f"TEL;TYPE={t.lower()}:{num}")
    for num, _ in tels:
        if num.startswith("+") and SIP_DOMAIN:
            # SIP address on the Twilio domain: lets Linphone dial the contact directly and
            # show the contact's name for incoming calls (From = sip:+E164@domain).
            lines.append(f"IMPP:sip:{num}@{SIP_DOMAIN}")
    for addr, t in emails:
        lines.append(f"EMAIL;TYPE={t.lower()}:{addr}")
    if org:
        lines.append(f"ORG:{esc(org)}")
    lines.append("END:VCARD")
    return uid, "\r\n".join(lines) + "\r\n"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    src = Path(args[0]).expanduser()
    cards = parse_cards(load_vcf_text(src))
    out: dict[str, str] = {}
    skipped = 0
    for c in cards:
        r = convert(c)
        if r:
            out[r[0]] = r[1]
        else:
            skipped += 1
    print(f"Takeout contacts: {len(cards)}   usable: {len(out)}   skipped (no phone/email): {skipped}")
    if "--dry-run" in sys.argv:
        for v in list(out.values())[:5]:
            print(v)
        return
    ok, removed = push_to_radicale(out, prune="--prune" in sys.argv)
    print(f"Uploaded to {RADICALE}: {ok}" + (f"   removed stale: {removed}" if "--prune" in sys.argv else ""))


if __name__ == "__main__":
    main()
