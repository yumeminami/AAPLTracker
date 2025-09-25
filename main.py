"""Command line utility for checking Apple Store pickup availability in China.

The script focuses on the iPhone 17 Pro and iPhone 17 Pro Max families, but it
also exposes switches so that advanced users can query additional part numbers
if they become available.  Apple exposes a public JSON endpoint used by the
online store to display in-store pickup options.  We call that endpoint directly
and summarise the results in a human friendly format.

Example usage::

    python main.py --location "Shanghai"  # implicit default models

    # Explicitly list every store near Shenzhen
    python main.py --location "Shenzhen" --show-raw

The endpoint occasionally rate limits callers.  When that happens the script
prints a helpful message instead of crashing so that it can be used in cronjobs
or other automation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence


APPLE_CN_FULFILMENT_URL = "https://www.apple.com.cn/shop/fulfillment-messages"


@dataclass(slots=True)
class ModelQuery:
    """Metadata describing a pickup query for a specific model family."""

    label: str
    search_term: str
    part_numbers: Sequence[str] | None = None


@dataclass(slots=True)
class AvailabilityRecord:
    """One availability datapoint for a store/product combination."""

    model_label: str
    store_name: str
    store_number: str | None
    city: str | None
    part_number: str
    product_title: str | None
    pickup_status: str
    pickup_available: bool
    pickup_quote: str | None


DEFAULT_MODELS: tuple[ModelQuery, ...] = (
    ModelQuery(label="iPhone 17 Pro", search_term="iPhone 17 Pro"),
    ModelQuery(label="iPhone 17 Pro Max", search_term="iPhone 17 Pro Max"),
)


class AppleStorePickupClient:
    """Minimal HTTP client that wraps the Apple Store fulfilment API."""

    def __init__(self, base_url: str = APPLE_CN_FULFILMENT_URL, *, timeout: float = 10.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def build_url(
        self,
        *,
        location: str | None,
        store_code: str | None,
        search_term: str | None,
        parts: Sequence[str] | None,
    ) -> str:
        params: dict[str, str] = {"pl": "true", "mt": "regular"}
        if location:
            params["location"] = location
        if store_code:
            params["store"] = store_code
        if search_term:
            params["search"] = search_term
        if parts:
            for index, part in enumerate(parts):
                params[f"parts.{index}"] = part
        return f"{self.base_url}?{urllib.parse.urlencode(params)}"

    def fetch(
        self,
        *,
        location: str | None,
        store_code: str | None,
        search_term: str | None,
        parts: Sequence[str] | None,
    ) -> dict:
        """Fetch fulfilment data and return it as a JSON dictionary."""

        url = self.build_url(location=location, store_code=store_code, search_term=search_term, parts=parts)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # type: ignore[arg-type]
            if response.status != 200:  # pragma: no cover - defensive; urllib raises for non-200
                msg = f"Unexpected response {response.status} from Apple Store API"
                raise RuntimeError(msg)
            return json.load(response)


def iter_availability(
    payload: dict,
    *,
    model_label: str,
    part_filter: set[str] | None = None,
) -> Iterator[AvailabilityRecord]:
    """Yield :class:`AvailabilityRecord` entries from a fulfilment payload."""

    stores = (
        payload.get("body", {})
        .get("content", {})
        .get("pickupMessage", {})
        .get("stores", [])
    )
    if not isinstance(stores, Iterable):  # pragma: no cover - API contract guard
        return

    for store in stores:
        if not isinstance(store, dict):
            continue
        store_name = str(store.get("storeName") or store.get("retailStoreName") or "Unknown store")
        store_number = store.get("storeNumber")
        city = store.get("city") or store.get("address", {}).get("city") if isinstance(store.get("address"), dict) else None
        parts_availability = store.get("partsAvailability", {})
        if not isinstance(parts_availability, dict):
            continue
        for part_number, info in parts_availability.items():
            if part_filter and part_number not in part_filter:
                continue
            if not isinstance(info, dict):
                continue
            status_raw = info.get("pickupDisplay") or info.get("storePickupLabel") or "unknown"
            status = str(status_raw).lower()
            message = (
                info.get("pickupSearchQuote")
                or info.get("storePickupQuote")
                or info.get("productAvailabilityText")
                or info.get("storePickupQuoteShort")
            )
            title = info.get("storePickupProductTitle") or info.get("title")
            normalized_status = status.replace(" ", "")
            pickup_available = normalized_status in {"available", "availabletoday", "availablesoon"}
            if "not" in status or "unavailable" in status:
                pickup_available = False
            yield AvailabilityRecord(
                model_label=model_label,
                store_name=store_name,
                store_number=str(store_number) if store_number is not None else None,
                city=str(city) if city is not None else None,
                part_number=str(part_number),
                product_title=str(title) if title is not None else None,
                pickup_status=status,
                pickup_available=pickup_available,
                pickup_quote=str(message) if message is not None else None,
            )


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Apple Store pick-up availability for the iPhone 17 Pro family in China.",
    )
    parser.add_argument(
        "--location",
        default="Beijing",
        help="City or postal code in China to search near. Defaults to Beijing.",
    )
    parser.add_argument(
        "--store",
        dest="store_code",
        help="Optional Apple Store code (e.g. R320) to query directly.",
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        choices=[model.label for model in DEFAULT_MODELS],
        help=(
            "Restrict the search to a particular model. "
            "Can be passed multiple times. Defaults to both iPhone 17 Pro variants."
        ),
    )
    parser.add_argument(
        "--part",
        dest="parts",
        action="append",
        help="Explicit Apple part number(s) to filter for (e.g. MTUV3CH/A).",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=0,
        help="Number of retry attempts when the Apple endpoint temporarily fails.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="Seconds to wait between retries (default: 2.0).",
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="Print the raw JSON payload for debugging purposes.",
    )
    return parser


def resolve_model_queries(selected_labels: Sequence[str] | None) -> list[ModelQuery]:
    if not selected_labels:
        return list(DEFAULT_MODELS)
    label_to_model = {model.label: model for model in DEFAULT_MODELS}
    return [label_to_model[label] for label in selected_labels]


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_argument_parser()
    args = parser.parse_args(argv)

    client = AppleStorePickupClient()
    queries = resolve_model_queries(args.models)

    cli_parts = list(args.parts) if args.parts else []

    for query in queries:
        print(f"\n=== {query.label} ===")
        retries_remaining = args.retry + 1
        payload = None
        query_parts = list(query.part_numbers) if query.part_numbers else []
        requested_parts: list[str] | None = query_parts or None
        if cli_parts:
            requested_parts = list(dict.fromkeys((requested_parts or []) + cli_parts))
        while retries_remaining > 0:
            retries_remaining -= 1
            try:
                payload = client.fetch(
                    location=args.location,
                    store_code=args.store_code,
                    search_term=query.search_term,
                    parts=requested_parts,
                )
                break
            except OSError as exc:
                if retries_remaining == 0:
                    print(f"Failed to contact Apple Store API: {exc}", file=sys.stderr)
                    return 1
                time.sleep(args.retry_delay)

        if payload is None:
            print("No data returned from Apple Store API.")
            continue

        if args.show_raw:
            print(json.dumps(payload, ensure_ascii=False, indent=2))

        combined_filter: set[str] | None = None
        if query_parts:
            combined_filter = set(query_parts)
        if cli_parts:
            cli_filter = set(cli_parts)
            combined_filter = cli_filter if combined_filter is None else combined_filter & cli_filter
            if query_parts and not combined_filter:
                print("No overlapping part numbers between the model defaults and --part filter.")
                continue

        records = list(
            iter_availability(payload, model_label=query.label, part_filter=combined_filter)
        )
        if not records:
            print("No stores returned by the API for this query.")
            continue

        for record in records:
            status_symbol = "✅" if record.pickup_available else "❌"
            location_bits = [record.store_name]
            if record.city:
                location_bits.append(record.city)
            if record.store_number:
                location_bits.append(f"#{record.store_number}")
            location_display = " - ".join(location_bits)
            product = record.product_title or record.model_label
            extra = record.pickup_quote or record.pickup_status
            print(f"{status_symbol} {location_display} | {product} ({record.part_number}) | {extra}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
