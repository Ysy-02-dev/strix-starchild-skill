"""Reading markets and prices. No credentials required.

The one genuinely subtle thing here is complement folding, in `strix_quote`.
A binary market's two outcomes are the same bet from opposite sides, so an ask
of 38c on No *is* a bid of 62c on Yes:

    complement bid at Q  ->  this side's ask at (1 - Q)
    complement ask at Q  ->  this side's bid at (1 - Q)

This matters because makers on this venue rest BUY orders on both tokens and
no SELL orders, so the raw book for one token has an empty ask side. Reporting
that unfolded shows "bid 60c / ask -" on a market whose real spread is a cent
wide, and an agent would reasonably conclude the market is untradeable.

Verified against staging on 2026-09-04: every book came back with
`normalized: true`, meaning that deployment already folds the complement
server-side and returns both sides. The client-side fold is therefore usually
a no-op there, and is kept as the fallback for a deployment or version that
does not normalize. Which is exactly why the `normalized` check is the
load-bearing half: folding an already-folded book would double-count every
level.

Ported from the production Telegram bot's `mirrorComplement`
(`src/strix/trading-gateway.ts`).
"""

from __future__ import annotations

from typing import Any

from .client import StrixError, format_price
from .session import resolve_client

OUTCOME_NO = 0
OUTCOME_YES = 1


def _as_dict(value: Any) -> dict:
    """Coerce a payload to a dict; the venue can answer with ``data: null``."""
    return value if isinstance(value, dict) else {}


def _round8(value: float) -> float:
    """Prices are compared as keys, so they must round-trip identically."""
    return round(value, 8)


def _best(levels: list[dict], pick: str) -> tuple[float | None, float]:
    """Best price and its size from one side of a book."""
    if not levels:
        return None, 0.0
    chooser = max if pick == "bid" else min
    best = chooser(levels, key=lambda level: float(level["price"]))
    return _round8(float(best["price"])), float(best.get("size", 0))


def _read_book(client, token_id: str, optional: bool = False) -> dict | None:
    """Read one token's book.

    ``optional`` is for the complement only: a complement we cannot read must
    not blank a book we can. The token's own book propagates its error, so a
    401 or a 429 reaches the caller with the status it needs to reconnect or
    back off rather than as a generic "could not read".
    """
    try:
        return client.request("GET", f"/orderbook/{token_id}", params={"depth": 50})
    except StrixError:
        if optional:
            return None
        raise


def strix_quote(token_id: str, complement_token_id: str = "") -> dict:
    """Best bid and ask for an outcome token.

    Pass ``complement_token_id`` wherever it is known — without it this reports
    only the token's own book, which on this venue usually has no ask side at
    all. ``strix_market`` returns both token ids for exactly this reason.
    """
    client = resolve_client()
    own = _as_dict(_read_book(client, token_id))

    bids = own.get("bids") or []
    asks = own.get("asks") or []
    best_bid, bid_size = _best(bids, "bid")
    best_ask, ask_size = _best(asks, "ask")

    folded = False
    if complement_token_id and not own.get("normalized"):
        other = _read_book(client, complement_token_id, optional=True)
        if other is not None:
            other_bid, other_bid_size = _best(other.get("bids") or [], "bid")
            other_ask, other_ask_size = _best(other.get("asks") or [], "ask")

            # complement ask at Q -> our bid at (1 - Q)
            mirrored_bid = None if other_ask is None else _round8(1 - other_ask)
            # complement bid at Q -> our ask at (1 - Q)
            mirrored_ask = None if other_bid is None else _round8(1 - other_bid)

            if mirrored_bid is not None and (best_bid is None or mirrored_bid > best_bid):
                best_bid, bid_size, folded = mirrored_bid, other_ask_size, True
            if mirrored_ask is not None and (best_ask is None or mirrored_ask < best_ask):
                best_ask, ask_size, folded = mirrored_ask, other_bid_size, True

    spread = None
    if best_bid is not None and best_ask is not None:
        spread = _round8(best_ask - best_bid)

    return {
        "token_id": token_id,
        "best_bid": best_bid,
        "bid_size": bid_size,
        "best_ask": best_ask,
        "ask_size": ask_size,
        "spread": spread,
        "complement_folded": folded,
        "summary": _quote_summary(best_bid, best_ask),
    }


def _quote_summary(best_bid: float | None, best_ask: float | None) -> str:
    return f"bid {format_price(best_bid)} / ask {format_price(best_ask)}"


def strix_search_markets(query: str = "", status: str = "open", limit: int = 20) -> dict:
    """Find markets by title.

    ``status`` is one of ``open``, ``closed``, ``resolved``. The venue matches
    ``query`` as a case-insensitive substring of the title.
    """
    client = resolve_client()
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
    if query:
        params["search"] = query
    if status:
        params["status"] = status

    page = _as_dict(client.request("GET", "/events", params=params))
    items = page.get("items") or []
    return {
        "count": len(items),
        "total": page.get("total"),
        "markets": [_summarise_event(event) for event in items],
    }


def _summarise_event(event: dict) -> dict:
    return {
        "event_id": event.get("id"),
        "slug": event.get("slug"),
        "title": event.get("title"),
        "status": (event.get("status") or "").upper(),
        "market_type": event.get("marketType"),
        "expiry_time": event.get("expiryTime"),
        "volume": event.get("volume"),
        "market_count": len(event.get("markets") or []),
    }


def strix_market(id_or_slug: str) -> dict:
    """One market with its outcome tokens.

    The ``token_id`` of an outcome is the key for quoting, ordering and
    positions — market ids and outcome ids are not interchangeable with it.
    """
    client = resolve_client()
    event = _as_dict(client.request("GET", f"/events/{id_or_slug}"))

    markets = []
    for market in event.get("markets") or []:
        outcomes = sorted(
            market.get("outcomes") or [],
            key=lambda outcome: outcome.get("outcomeIndex", 0),
        )
        markets.append(
            {
                "market_id": market.get("id"),
                "title": market.get("title") or event.get("title"),
                "status": (market.get("status") or "").upper(),
                "volume": market.get("volume"),
                "winning_outcome": (market.get("winningOutcome") or {}).get("name"),
                "outcomes": [
                    {
                        "name": outcome.get("name"),
                        "token_id": outcome.get("tokenId"),
                        "outcome_index": outcome.get("outcomeIndex"),
                        "last_price": _as_float(outcome.get("lastPrice")),
                        "side": "YES"
                        if outcome.get("outcomeIndex") == OUTCOME_YES
                        else "NO",
                    }
                    for outcome in outcomes
                ],
            }
        )

    result = {
        "event_id": event.get("id"),
        "slug": event.get("slug"),
        "title": event.get("title"),
        "status": (event.get("status") or "").upper(),
        "market_type": event.get("marketType"),
        "tick_size": _as_float(event.get("tickSize")),
        "expiry_time": event.get("expiryTime"),
        "rule": event.get("rule"),
        "market_count": len(markets),
        "is_multi_market": len(markets) > 1,
        "markets": markets,
        "note": "outcome_index 0 is No and 1 is Yes. Quote and trade using token_id.",
    }

    # A NEG_RISK event holds one sub-market per candidate — five films, say —
    # and their order is not stable between requests. Taking markets[0] can
    # therefore trade a different one than was just inspected, which happened
    # during live testing. Say so rather than let an agent guess.
    if result["is_multi_market"]:
        result["warning"] = (
            f"This event has {len(markets)} sub-markets, one per outcome: "
            f"{', '.join(m['title'] or '?' for m in markets)}. "
            "Their order is not stable between requests, so do NOT take "
            "markets[0] — choose the one the user meant by title with "
            "strix_find_submarket, or by its market_id."
        )

    return result


def strix_find_submarket(id_or_slug: str, market_query: str) -> dict:
    """Pick one sub-market of a multi-outcome event by title, or by market id.

    Matching is case-insensitive and partial. An ambiguous or unknown query
    raises rather than guessing — picking the wrong sub-market means trading
    the wrong outcome.
    """
    event = strix_market(id_or_slug)
    candidates = event["markets"]
    query = (market_query or "").strip().lower()
    if not query:
        raise ValueError("market_query is required — name the sub-market to trade.")

    exact = [m for m in candidates if (m.get("market_id") or "").lower() == query]
    if len(exact) == 1:
        return exact[0]

    matches = [m for m in candidates if query in (m.get("title") or "").lower()]
    available = ", ".join(m.get("title") or "?" for m in candidates)

    if not matches:
        raise ValueError(
            f"No sub-market of {event['title']!r} matches {market_query!r}. "
            f"Available: {available}."
        )
    if len(matches) > 1:
        raise ValueError(
            f"{market_query!r} matches {len(matches)} sub-markets "
            f"({', '.join(m['title'] or '?' for m in matches)}). Be more specific."
        )
    return matches[0]


def strix_orderbook(token_id: str, depth: int = 20) -> dict:
    """Raw order book for one outcome token.

    This is the venue's book for this token alone. It commonly has an empty ask
    side — see ``strix_quote`` for the tradeable price with the complement
    folded in.
    """
    client = resolve_client()
    book = _as_dict(
        client.request(
            "GET",
            f"/orderbook/{token_id}",
            params={"depth": max(1, min(int(depth), 200))},
        )
    )
    return {
        "token_id": token_id,
        "bids": book.get("bids", []),
        "asks": book.get("asks", []),
        "normalized": bool(book.get("normalized")),
        "note": (
            "Asks are often empty because makers rest BUY orders on both "
            "outcomes. Use strix_quote for the real tradeable price."
        ),
    }


def strix_leaderboard(period: str = "weekly", limit: int = 20) -> dict:
    """Top traders by volume for a period (``today``/``weekly``/``monthly``/``all``)."""
    client = resolve_client()
    result = client.request(
        "GET", "/leaderboard", params={"period": period, "limit": limit}
    )
    items = result if isinstance(result, list) else (result or {}).get("items", [])
    return {"period": period, "traders": items}


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
