#!/usr/bin/env python3
"""Decode mbox files: proper MIME/quoted-printable parsing, print threads."""
import mailbox
import sys

for path in sys.argv[1:]:
    print("=" * 70)
    print("FILE:", path)
    print("=" * 70)
    for msg in mailbox.mbox(path):
        print("-" * 70)
        print("From:    ", msg.get("From", ""))
        print("Date:    ", msg.get("Date", ""))
        print("Subject: ", msg.get("Subject", ""))
        print("In-Reply-To:", (msg.get("In-Reply-To", "") or "")[:80])
        print("-" * 70)
        part = msg
        if msg.is_multipart():
            for p in msg.walk():
                if p.get_content_type() == "text/plain":
                    part = p
                    break
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = str(part).encode()
        text = payload.decode(part.get_content_charset() or "utf-8",
                              errors="replace")
        print(text)
