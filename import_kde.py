#!/usr/bin/env python3
"""Import the phone contacts cached by KDE Connect into the Linphone CardDAV
address book, skipping people whose phone numbers are already there from Google.

Usage: import_kde.py [--all] [--dry-run]
  --all      also import contacts whose every number already exists (default: skip them)
"""
import glob
import hashlib
import os
import quopri
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_takeout import convert, parse_cards  # noqa: E402
from sync import RADICALE, RADICALE_USER, push_to_radicale  # noqa: E402

KDE_DIR = glob.glob(os.path.expanduser("~/.local/share/kpeoplevcard/kdeconnect-*"))


def decode_qp(text: str) -> str:
    """vCard 2.1 QUOTED-PRINTABLE (with soft line breaks) -> plain UTF-8 lines."""
    out = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.upper().startswith("PHOTO"):
            # drop the photo and its folded continuation lines (base64)
            while i + 1 < len(lines) and lines[i + 1][:1] in (" ", "\t"):
                i += 1
            i += 1
            continue
        if "ENCODING=QUOTED-PRINTABLE" in line.upper():
            while line.endswith("=") and i + 1 < len(lines):
                i += 1
                line = line[:-1] + lines[i]
            head, _, val = line.partition(":")
            head = ";".join(p for p in head.split(";") if not p.upper().startswith(("ENCODING=", "CHARSET=")))
            line = head + ":" + quopri.decodestring(val.encode()).decode("utf-8", "replace")
        out.append(line)
        i += 1
    return "\n".join(out)


def existing_numbers() -> set[str]:
    import requests

    r = requests.request("PROPFIND", RADICALE, headers={"X-Remote-User": RADICALE_USER, "Depth": "1"}, timeout=60)
    hrefs = re.findall(r"<href>([^<]+\.vcf)</href>", r.text)
    hrefs = [h for h in hrefs if not h.rsplit("/", 1)[-1].startswith("kde-")]  # dedupe against non-KDE entries only
    nums: set[str] = set()
    for h in hrefs:
        v = requests.get(RADICALE + h.rsplit("/", 1)[-1], headers={"X-Remote-User": RADICALE_USER}, timeout=30).text
        nums.update(re.findall(r"^TEL[^:]*:(\S+)", v, re.M))
    return nums


def main():
    if not KDE_DIR:
        sys.exit("No KDE Connect contact cache found")
    text = "\n".join(decode_qp(Path(f).read_text("utf-8", "replace")) for f in sorted(glob.glob(KDE_DIR[0] + "/*.vcf")))
    cards = parse_cards(text)
    have = set() if "--all" in sys.argv else existing_numbers()
    out: dict[str, str] = {}
    skipped_dup = skipped_empty = 0
    for c in cards:
        r = convert(c)
        if not r:
            skipped_empty += 1
            continue
        uid, vcf = r
        tels = re.findall(r"^TEL[^:]*:(\S+)", vcf, re.M)
        if tels and all(t in have for t in tels):
            skipped_dup += 1
            continue
        kde_id = next((l.split(":", 1)[1] for l in c if l.upper().startswith("X-KDECONNECT-ID")), uid)
        uid = "kde-" + hashlib.sha1(kde_id.encode()).hexdigest()[:24]
        vcf = re.sub(r"^UID:.*$", f"UID:{uid}", vcf, count=1, flags=re.M)
        out[uid] = vcf
    print(f"KDE Connect contacts: {len(cards)}   new: {len(out)}   already present: {skipped_dup}   no phone/email: {skipped_empty}")
    if "--dry-run" in sys.argv:
        for v in list(out.values())[:4]:
            print(v)
        return
    ok, _ = push_to_radicale(out, prune=False)
    print(f"Uploaded to {RADICALE}: {ok}")


if __name__ == "__main__":
    main()
