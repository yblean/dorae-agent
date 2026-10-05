"""Split a Google Takeout .mbox into numbered .eml files in data/emails/.

Emails already in the folder (same Message-ID) are skipped, so re-exporting
the whole label and importing again only adds the new ones.

    python -m evals.import_mbox path/to/All\\ mail.mbox --limit 300
"""
import argparse
import mailbox
from email import policy
from email.parser import BytesParser
from pathlib import Path


def _message_id(raw: bytes) -> str:
    headers = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return str(headers["Message-ID"] or "").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mbox", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/emails"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    existing = sorted(args.out.glob("*.eml"))
    seen = {_message_id(p.read_bytes()) for p in existing} - {""}
    next_num = max((int(p.stem) for p in existing if p.stem.isdigit()), default=0) + 1

    added = skipped = 0
    for msg in mailbox.mbox(args.mbox):
        raw = msg.as_bytes()
        mid = _message_id(raw)
        if mid and mid in seen:
            skipped += 1
            continue
        seen.add(mid)
        path = args.out / f"{next_num:04d}.eml"
        path.write_bytes(raw)
        print(f"added {path.name}")
        next_num += 1
        added += 1
        if args.limit and added >= args.limit:
            break
    print(f"added {added}, skipped {skipped} already imported")


if __name__ == "__main__":
    main()
