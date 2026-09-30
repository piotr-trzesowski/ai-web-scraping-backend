"""Discover and ingest upcoming Gdańsk techno events outside API requests."""

import argparse
import logging
import time
from datetime import datetime
from urllib.parse import urldefrag

from openai import APIError, OpenAI
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.database import session_factory
from app.ai_event_extraction import extract_events
from app.safe_page_fetcher import FetchError, PageFetcher
from app.event_repository import save_event
from app.timeframe import WARSAW, window

logger = logging.getLogger(__name__)


def discover(client: OpenAI, model: str, start: datetime, end: datetime) -> list[str]:
    response = client.responses.create(
        model=model,
        tools=[{"type": "web_search", "search_context_size": "medium"}],
        input=(
            "Search the live web for specific upcoming techno music events in Gdańsk, Poland, "
            f"between {start.date()} and {end.date()}. Cite the event organizer, venue or "
            "ticket page for each possible event; no past events or generic listings. "
            "Search both Polish and English. Return short sourced findings."
        ),
    )
    if not any(item.type == "web_search_call" for item in response.output):
        raise ValueError("OpenAI did not perform web search")
    urls: list[str] = []
    for item in response.output:
        if item.type != "message":
            continue
        for content in item.content:
            for annotation in getattr(content, "annotations", []):
                if annotation.type == "url_citation":
                    url = urldefrag(annotation.url)[0]
                    if url.startswith(("http://", "https://")) and url not in urls:
                        urls.append(url)
    if not urls:
        raise ValueError("Web search supplied no cited pages to verify")
    return urls[:12]


def run_once(months: int = 3) -> int:
    settings = get_settings()
    if not settings.openai_api_key or not settings.openai_api_key.get_secret_value():
        raise ValueError("Set OPENAI_API_KEY before running ingestion")
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value(), timeout=30, max_retries=2)
    start, end = window(months)
    urls = discover(client, settings.openai_search_model, start, end)
    count = 0
    errors = 0
    with PageFetcher() as fetcher:
        for url in urls:
            try:
                html = fetcher.fetch(url)
                events = extract_events(client, settings.openai_model, html, url)
                with session_factory().begin() as session:
                    for event in events:
                        if not start <= event.starts_at.astimezone(WARSAW) < end:
                            continue
                        key = f"{url}|{event.title.casefold()}|{event.starts_at.isoformat()}"
                        save_event(session, event, "web-search", key)
                        count += 1
            except (APIError, FetchError, ValueError, SQLAlchemyError):
                errors += 1
                logger.exception("Could not ingest %s", url)
    logger.info("Saved %d events from %d cited pages; %d pages failed", count, len(urls), errors)
    if errors == len(urls):
        raise ValueError("All cited pages failed ingestion")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Find upcoming Gdańsk techno events")
    parser.add_argument("--months", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument("--interval", type=int, help="Repeat every N seconds (at least 3600)")
    args = parser.parse_args()
    if args.interval is not None and args.interval < 3600:
        parser.error("--interval must be at least 3600 seconds")
    logging.basicConfig(level=logging.INFO)
    while True:
        run_once(args.months)
        if args.interval is None:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
