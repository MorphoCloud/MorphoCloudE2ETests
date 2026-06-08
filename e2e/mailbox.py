"""IMAP inbox polling + structural parsers for credential emails.

The harness asserts the *real* credential email arrives (Decision 4). Assertions are
structural — a connection URL is present, a passphrase is present, the workshop CSV has
N rows — not exact prose, so they survive copy edits.
"""

from __future__ import annotations

import csv
import email
import imaplib
import io
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from typing import Callable

from . import config

# Guacamole connection URL the workflow emails (generate-connection-url).
_URL_RE = re.compile(r"https://[^\s\"'<>)]+", re.I)
_PASSPHRASE_RE = re.compile(
    r"(?:passphrase|password)\s*[:=]\s*(?P<v>\S+)", re.I
)


@dataclass
class MailMessage:
    subject: str
    from_addr: str
    to_addrs: str
    text: str
    attachments: dict[str, bytes] = field(default_factory=dict)
    raw: Message | None = None

    # -- structural parsers ------------------------------------------------------------

    def connection_url(self) -> str | None:
        m = _URL_RE.search(self.text)
        return m.group(0) if m else None

    def passphrase(self) -> str | None:
        m = _PASSPHRASE_RE.search(self.text)
        return m.group("v") if m else None

    def credential_csv_rows(self) -> list[list[str]]:
        """Parse the workshop credential CSV (attachment or inline) → data rows.

        Returns rows excluding a header line if one is detected. Used to assert the
        organizer email lists the expected number of instances.
        """
        blob = None
        for name, data in self.attachments.items():
            if name.lower().endswith(".csv"):
                blob = data.decode("utf-8", "replace")
                break
        if blob is None:
            # Fall back to an inline CSV-looking block (comma-separated, >=3 cols).
            lines = [ln for ln in self.text.splitlines() if ln.count(",") >= 2]
            blob = "\n".join(lines)
        if not blob.strip():
            return []
        rows = list(csv.reader(io.StringIO(blob)))
        if rows and any(h.lower() in ("instance", "name", "instance name")
                        for h in (rows[0][0:1] or [""])):
            rows = rows[1:]
        return [r for r in rows if any(cell.strip() for cell in r)]


class Mailbox:
    """IMAP poller. Use as a context manager; `since` filters to fresh mail."""

    def __init__(self):
        if not config.imap_configured():
            raise RuntimeError("IMAP is not configured (E2E_IMAP_* env)")
        self.host = config.IMAP_HOST
        self.port = config.IMAP_PORT
        self.user = config.IMAP_USER
        self.password = config.IMAP_PASSWORD
        self.mailbox = config.IMAP_MAILBOX
        self._imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "Mailbox":
        self._imap = imaplib.IMAP4_SSL(self.host, self.port)
        self._imap.login(self.user, self.password)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._imap is not None:
                self._imap.logout()
        except Exception:
            pass

    def _fetch_recent(self, since: datetime) -> list[MailMessage]:
        assert self._imap is not None
        self._imap.select(self.mailbox)
        # IMAP SINCE granularity is a day; we re-filter precisely by Date header below.
        date_str = since.astimezone(timezone.utc).strftime("%d-%b-%Y")
        typ, data = self._imap.search(None, "SINCE", date_str)
        if typ != "OK" or not data or not data[0]:
            return []
        out: list[MailMessage] = []
        for num in data[0].split():
            typ, msg_data = self._imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            out.append(_parse(email.message_from_bytes(msg_data[0][1])))
        return out

    def wait_for(
        self,
        predicate: Callable[[MailMessage], bool],
        *,
        since: datetime,
        timeout: float = config.TIMEOUT_EMAIL,
        interval: float = 10.0,
    ) -> MailMessage:
        """Poll until a message matching `predicate` (and newer than `since`) arrives."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for msg in self._fetch_recent(since):
                if predicate(msg):
                    return msg
            time.sleep(interval)
        raise TimeoutError(f"No matching email within {timeout:.0f}s (since {since.isoformat()})")


def _parse(msg: Message) -> MailMessage:
    text_parts: list[str] = []
    attachments: dict[str, bytes] = {}
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if "attachment" in disp.lower() or part.get_filename():
                attachments[part.get_filename() or "attachment"] = payload
            elif ctype in ("text/plain", "text/html"):
                text_parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            text_parts.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
    return MailMessage(
        subject=str(msg.get("Subject", "")),
        from_addr=str(msg.get("From", "")),
        to_addrs=str(msg.get("To", "")),
        text="\n".join(text_parts),
        attachments=attachments,
        raw=msg,
    )
