#!/usr/bin/env python3
"""Monitor JRT's repertoire and email when Riga tickets become available."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


REPERTOIRE_URL = "https://www.jrt.lv/repertuars/"
RESEND_URL = "https://api.resend.com/emails"
RIGA_VENUE = "rīga, lāčplēša iela 25"
USER_AGENT = "jrt-ticket-monitor/1.0 (+https://github.com/)"


@dataclass(frozen=True)
class Performance:
    title: str
    date: str
    time: str
    address: str
    url: str
    sold_out: bool

    @property
    def event_id(self) -> str:
        value = "\n".join((self.title, self.date, self.time, self.address, self.url))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


class RepertoireParser(HTMLParser):
    """Parse the server-rendered JRT event cards without third-party packages."""

    _FIELDS = {
        "title": "title",
        "day_small": "date",
        "laiks": "time",
        "address": "address",
        "izpardots": "sold_out_text",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.performances: list[Performance] = []
        self._depth = 0
        self._row_depth: int | None = None
        self._row: dict[str, Any] | None = None
        self._captures: list[tuple[int, str, str, list[str]]] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        values = dict(attrs).get("class") or ""
        return set(values.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        classes = self._classes(attrs)

        if self._row is None and tag == "div" and "row-wrapper" in classes:
            self._row_depth = self._depth
            self._row = {"links": [], "sold_out": False}

        if self._row is None:
            return

        href = dict(attrs).get("href")
        if tag == "a" and href:
            self._row["links"].append(href)

        for class_name, field in self._FIELDS.items():
            if class_name in classes:
                self._captures.append((self._depth, tag, field, []))
                break

    def handle_data(self, data: str) -> None:
        if self._captures:
            self._captures[-1][3].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._captures:
            depth, capture_tag, field, parts = self._captures[-1]
            if depth == self._depth and capture_tag == tag:
                self._captures.pop()
                if self._row is not None:
                    value = " ".join("".join(parts).split())
                    if field == "sold_out_text":
                        self._row["sold_out"] = True
                    else:
                        self._row[field] = value

        if self._row is not None and self._row_depth == self._depth and tag == "div":
            self._finish_row()

        self._depth = max(0, self._depth - 1)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def _finish_row(self) -> None:
        assert self._row is not None
        links = self._row.get("links", [])
        external = next((link for link in links if link.startswith("http")), None)
        link = external or (links[0] if links else REPERTOIRE_URL)

        if self._row.get("title") and self._row.get("address"):
            self.performances.append(
                Performance(
                    title=self._row["title"],
                    date=self._row.get("date", ""),
                    time=self._row.get("time", ""),
                    address=self._row["address"],
                    url=urljoin(REPERTOIRE_URL, link),
                    sold_out=bool(self._row.get("sold_out")),
                )
            )

        self._row = None
        self._row_depth = None
        self._captures.clear()


def parse_repertoire(document: str) -> list[Performance]:
    parser = RepertoireParser()
    parser.feed(document)
    parser.close()
    return parser.performances


def fetch_text(url: str, attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            )
            with urlopen(request, timeout=25) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def is_riga_venue(performance: Performance) -> bool:
    return RIGA_VENUE in " ".join(performance.address.lower().split())


def available_in_riga(performances: list[Performance]) -> list[Performance]:
    return [event for event in performances if is_riga_venue(event) and not event.sold_out]


def load_state(path: Path) -> set[str]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return set(state.get("available_event_ids", []))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError, TypeError) as error:
        raise RuntimeError(f"Invalid state file {path}: {error}") from error


def save_state(path: Path, events: list[Performance]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "available_event_ids": sorted(event.event_id for event in events),
        "events": [asdict(event) for event in events],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def make_message(events: list[Performance], *, test: bool = False) -> tuple[str, str, str]:
    prefix = "[TEST] " if test else ""
    if events:
        subject = f"{prefix}JRT: появились билеты ({len(events)})"
        text_lines = ["На эти спектакли JRT в Риге сейчас есть билеты:", ""]
        html_rows = []
        for event in events:
            when = " ".join(part for part in (event.date, event.time) if part)
            text_lines.extend((f"{event.title} — {when}", event.address, event.url, ""))
            html_rows.append(
                "<li><strong>{}</strong><br>{}<br>{}<br>"
                '<a href="{}">Открыть билеты</a></li>'.format(
                    html.escape(event.title),
                    html.escape(when),
                    html.escape(event.address),
                    html.escape(event.url, quote=True),
                )
            )
        html_body = (
            "<p>На эти спектакли JRT в Риге сейчас есть билеты:</p><ul>"
            + "".join(html_rows)
            + "</ul>"
        )
        return subject, "\n".join(text_lines), html_body

    subject = f"{prefix}JRT monitor работает"
    text_body = "Проверка прошла успешно. Сейчас доступных билетов на Lāčplēša iela 25 нет."
    html_body = f"<p>{html.escape(text_body)}</p>"
    return subject, text_body, html_body


def send_email(events: list[Performance], *, test: bool = False) -> str:
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("EMAIL_TO")
    sender = os.environ.get("EMAIL_FROM") or "JRT Monitor <onboarding@resend.dev>"
    missing = [name for name, value in (("RESEND_API_KEY", api_key), ("EMAIL_TO", recipient)) if not value]
    if missing:
        raise RuntimeError(f"Missing email configuration: {', '.join(missing)}")

    subject, text_body, html_body = make_message(events, test=test)
    payload = json.dumps(
        {"from": sender, "to": [recipient], "subject": subject, "text": text_body, "html": html_body}
    ).encode("utf-8")

    if test:
        unique = os.environ.get("GITHUB_RUN_ID") or str(time.time_ns())
        idempotency_key = f"jrt-test-{unique}"
    else:
        identity = ",".join(sorted(event.event_id for event in events))
        idempotency_key = "jrt-availability-" + hashlib.sha256(identity.encode()).hexdigest()

    request = Request(
        RESEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not send email: {error}") from error
    return str(result.get("id", "unknown"))


def read_document(path: Path | None) -> str:
    return path.read_text(encoding="utf-8") if path else fetch_text(REPERTOIRE_URL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="parse and print; do not email or update state")
    parser.add_argument("--force-email", action="store_true", help="send a test email even when nothing is available")
    parser.add_argument("--html-file", type=Path, help="read HTML from a local fixture instead of JRT")
    parser.add_argument("--state-file", type=Path, default=Path(".state/last_seen.json"))
    args = parser.parse_args(argv)

    performances = parse_repertoire(read_document(args.html_file))
    riga_events = [event for event in performances if is_riga_venue(event)]
    if not performances or not riga_events:
        raise RuntimeError(
            "JRT page structure may have changed: "
            f"parsed {len(performances)} total and {len(riga_events)} Riga performances"
        )

    available = available_in_riga(performances)
    print(
        f"Parsed {len(performances)} performances; {len(riga_events)} in Riga; "
        f"{len(available)} with tickets."
    )
    for event in available:
        print(f"AVAILABLE: {event.date} {event.time} — {event.title} — {event.url}")

    if args.check_only:
        return 0

    previous_ids = load_state(args.state_file)
    new_events = [event for event in available if event.event_id not in previous_ids]

    if args.force_email:
        message_id = send_email(available, test=True)
        print(f"Test email sent (id: {message_id}).")
    elif new_events:
        message_id = send_email(new_events)
        print(f"Notification sent for {len(new_events)} new performance(s) (id: {message_id}).")
    else:
        print("No newly available performances; no email sent.")

    save_state(args.state_file, available)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
