"""Placing, cancelling and settling orders. Requires a connected credential.

Validation that the venue would reject anyway is done locally first, so an
agent gets a sentence it can act on instead of a 400. Nothing here retries a
failed order as a *different* order: when the venue refuses, the refusal is
returned with an explanation. Silently substituting a trade the agent did not
ask for is not a behaviour worth having in a trading tool.
"""

from __future__ import annotations

import math
from typing import Any

from .client import format_price
from .session import resolve_client

MIN_ORDER_VALUE_USD = 1.0
MIN_PRICE = 0.0001
MAX_PRICE = 0.9999
DEFAULT_TICK = 0.01
VALID_SIDES = ("BUY", "SELL")
VALID_ORDER_TYPES = ("GTC", "GTD", "FOK", "FAK")


def _as_dict(value: Any) -> dict:
    """Coerce a payload to a dict.

    Applied at every response boundary rather than case by case: the venue can
    answer with ``data: null``, and a crash after a POST has already succeeded
    reads to an agent as a failure it should retry — which doubles a position.
    """
    return value if isinstance(value, dict) else {}


def _as_list(value: Any, key: str = "items") -> list:
    """Coerce a payload to a list, accepting either a bare array or an envelope."""
    if isinstance(value, list):
        return value
    return _as_dict(value).get(key) or []


def _check_side(side: str) -> str:
    upper = (side or "").upper()
    if upper not in VALID_SIDES:
        raise ValueError(f"side must be BUY or SELL, got {side!r}.")
    return upper


def _check_price(price: float, tick_size: float | None) -> float:
    value = float(price)
    if not MIN_PRICE <= value <= MAX_PRICE:
        raise ValueError(
            f"price must be between {MIN_PRICE} and {MAX_PRICE} "
            f"(a probability, so 0.65 means 65%), got {price}."
        )

    # Tick size varies per market (0.01, some 0.001). Validating against a
    # guessed tick would reject prices the venue accepts, so this check only
    # runs when the caller supplies the market's real tick from strix_market.
    if tick_size is None:
        return round(value, 8)

    tick = float(tick_size)
    if tick <= 0:
        raise ValueError(f"tick_size must be greater than zero, got {tick_size}.")

    # Work in integer tick units so binary-float drift cannot fail a valid price.
    lower = math.floor(round(value / tick, 8))
    if abs(round(lower * tick, 8) - value) > 1e-9 and abs(
        round((lower + 1) * tick, 8) - value
    ) > 1e-9:
        # Clamp the suggestions to the tradeable range so the message never
        # points at 0.0, which the venue would refuse for a different reason.
        below = max(round(lower * tick, 8), MIN_PRICE)
        above = min(round((lower + 1) * tick, 8), MAX_PRICE)
        raise ValueError(
            f"price must be a multiple of the tick size {tick}, got {price}. "
            f"The nearest valid prices are {below} and {above}."
        )
    return round(value, 8)


def _check_order_value(value: float, what: str) -> float:
    amount = float(value)
    if amount < MIN_ORDER_VALUE_USD:
        # Show enough precision that the message cannot read as "$1.00 is below
        # the minimum of $1.00" for a value a hair under the threshold.
        shown = f"{amount:.2f}" if abs(amount - round(amount, 2)) < 1e-9 else f"{amount:.4g}"
        raise ValueError(
            f"{what} is ${shown}, below the venue minimum of "
            f"${MIN_ORDER_VALUE_USD:.2f} per order."
        )
    return amount


def strix_limit_order(
    token_id: str,
    side: str,
    price: float,
    quantity: float,
    order_type: str = "GTC",
    expires_at: str = "",
    tick_size: float | None = None,
) -> dict:
    """Rest a limit order on the book.

    ``price`` is a probability between 0.0001 and 0.9999 — 0.65 means 65c, or
    a 65% implied chance. ``quantity`` is a number of shares, each worth $1 if
    the outcome resolves true.

    Pass ``tick_size`` from ``strix_market``'s ``tick_size`` field to have the
    price checked locally. Left unset, tick alignment is validated by the venue
    instead — better than guessing a tick and refusing a price that is legal on
    a finer-grained market.
    """
    checked_side = _check_side(side)
    checked_price = _check_price(price, tick_size)
    shares = float(quantity)
    _check_order_value(checked_price * shares, "order value (price x quantity)")

    upper_type = (order_type or "GTC").upper()
    if upper_type not in VALID_ORDER_TYPES:
        raise ValueError(
            f"order_type must be one of {', '.join(VALID_ORDER_TYPES)}, got {order_type!r}."
        )
    if upper_type == "GTD" and not expires_at:
        raise ValueError("A GTD order needs expires_at as an ISO-8601 timestamp.")

    body: dict[str, Any] = {
        "tokenId": token_id,
        "side": checked_side,
        "price": checked_price,
        "quantity": shares,
        "orderType": upper_type,
    }
    if expires_at:
        body["expiredAt"] = expires_at

    client = resolve_client()
    order = client.request("POST", "/orders", body=body, authenticated=True)
    return _describe_order(order, checked_side, shares, checked_price)


def _market_size(value: float) -> str:
    """Render a market-order size the way the venue expects it.

    The endpoint takes ``maxSpend``/``quantity`` as strings, not numbers — the
    SDK sends ``String(size)``. Sending a number is rejected as "Invalid market
    order request", which reads like a liquidity problem and is not one.
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def strix_buy(
    token_id: str, usd_amount: float, time_in_force: str = "FOK"
) -> dict:
    """Buy an outcome at the market price, spending at most ``usd_amount``.

    ``time_in_force`` defaults to ``FOK`` — all or nothing — matching the SDK.
    Use ``FAK`` to take whatever is available instead of being rejected when
    the book is too thin to fill the whole amount.
    """
    spend = _check_order_value(usd_amount, "usd_amount")
    tif = (time_in_force or "FOK").upper()
    if tif not in ("FOK", "FAK"):
        raise ValueError(f"time_in_force for a market order must be FOK or FAK, got {time_in_force!r}.")

    client = resolve_client()
    result = client.request(
        "POST",
        "/orders/market",
        body={
            "tokenId": token_id,
            "side": "BUY",
            "maxSpend": _market_size(spend),
            "timeInForce": tif,
        },
        authenticated=True,
    )
    return _describe_market_order(result, "BUY")


def strix_sell(
    token_id: str, quantity: float, time_in_force: str = "FAK"
) -> dict:
    """Sell ``quantity`` shares of an outcome at the market price.

    ``time_in_force`` defaults to ``FAK`` — fill whatever is available now —
    because a partial exit is usually better than none.

    Unlike a buy, the $1 minimum order value cannot be checked here: the value
    of a sell is ``quantity x execution price``, and the execution price is not
    known until the venue walks the book. A sub-$1 sell is therefore refused by
    the venue rather than locally.
    """
    shares = float(quantity)
    if shares <= 0:
        raise ValueError(f"quantity must be greater than zero, got {quantity}.")
    tif = (time_in_force or "FAK").upper()
    if tif not in ("FOK", "FAK"):
        raise ValueError(f"time_in_force for a market order must be FOK or FAK, got {time_in_force!r}.")

    client = resolve_client()
    result = client.request(
        "POST",
        "/orders/market",
        body={
            "tokenId": token_id,
            "side": "SELL",
            "quantity": _market_size(shares),
            "timeInForce": tif,
        },
        authenticated=True,
    )
    return _describe_market_order(result, "SELL")


def strix_cancel_all(token_id: str = "", market_id: str = "") -> dict:
    """Cancel resting orders — everything, or scoped to one token or market.

    This is the kill switch, and the venue never rate-limits it.
    """
    params: dict[str, Any] = {}
    if token_id:
        params["tokenId"] = token_id
    if market_id:
        params["marketId"] = market_id

    client = resolve_client()
    result = client.request(
        "DELETE", "/orders", params=params or None, authenticated=True
    )
    cancelled = _as_dict(result).get("cancelled")
    return {
        "cancelled": cancelled,
        "scope": token_id or market_id or "all open orders",
    }


def strix_open_orders(token_id: str = "") -> dict:
    """Orders still resting on the book."""
    params = {"tokenId": token_id} if token_id else None
    client = resolve_client()
    orders = client.request("GET", "/orders", params=params, authenticated=True)
    items = _as_list(orders)
    return {"count": len(items), "orders": [_describe_resting(o) for o in items]}


def strix_status() -> dict:
    """Spendable USDC and share counts, in one call.

    Uses the balances endpoint rather than the portfolio summary: the summary
    replays the whole trade ledger to compute P&L, which a balance check never
    needs.
    """
    client = resolve_client()
    balances = _as_dict(client.request("GET", "/portfolio/balances", authenticated=True))
    positions = balances.get("positions") or []
    return {
        "available_usdc": _as_float(balances.get("availableBalance")),
        "position_count": len(positions),
        "positions": [
            {
                "token_id": position.get("tokenId"),
                "shares": _as_float(position.get("shareAmount")),
            }
            for position in positions
        ],
    }


def strix_positions() -> dict:
    """Open positions across every market, with P&L."""
    client = resolve_client()
    positions = client.request("GET", "/portfolio/positions", authenticated=True)
    items = _as_list(positions)
    return {"count": len(items), "positions": items}


def strix_claimable() -> dict:
    """Settled markets still holding shares, including ones that lost.

    A losing market pays nothing but the position stays open until it is
    redeemed, so ``payout`` of 0 is a real row rather than one to filter out.
    """
    client = resolve_client()
    claims = client.request("GET", "/positions/claimable", authenticated=True)
    items = _as_list(claims)
    return {
        "count": len(items),
        "total_payout": sum(_as_float(c.get("payout")) or 0 for c in items),
        "claims": items,
    }


def strix_redeem(market_id: str) -> dict:
    """Redeem a settled market's shares for USDC."""
    client = resolve_client()
    result = client.request(
        "POST", "/positions/redeem", body={"marketId": market_id}, authenticated=True
    )
    return {"market_id": market_id, "tx_hash": _as_dict(result).get("txHash")}


SETTLEMENT_NOTE = (
    "Matching and settlement are asynchronous, around 15-20 seconds. This "
    "response reporting status OPEN and zero filled does not mean the order "
    "missed — check strix_positions or strix_open_orders shortly."
)


def _describe_order(order: Any, side: str, quantity: float, price: float) -> dict:
    order = _as_dict(order)
    return {
        "order_hash": order.get("orderHash"),
        "status": order.get("status"),
        "side": side,
        "price": price,
        "quantity": quantity,
        "filled_quantity": _as_float(order.get("filledQuantity")) or 0,
        "summary": f"{side} {quantity:g} shares at {format_price(price)}",
        "note": SETTLEMENT_NOTE,
    }


def _describe_market_order(result: Any, side: str) -> dict:
    result = _as_dict(result)
    order = _as_dict(result.get("order") or result)
    plan = _as_dict(result.get("executionPlan")) or None
    described = {
        "order_hash": order.get("orderHash"),
        "status": order.get("status"),
        "side": side,
        "note": SETTLEMENT_NOTE,
    }
    if plan:
        described["execution_plan"] = {
            "quantity": _as_float(plan.get("submittedQuantity")),
            "limit_price": _as_float(plan.get("limitPrice")),
            "estimated_average_price": _as_float(plan.get("estimatedAveragePrice")),
            "estimated_quote_amount": _as_float(plan.get("estimatedQuoteAmount")),
        }
    return described


def _describe_resting(order: Any) -> dict:
    order = _as_dict(order)
    quantity = _as_float(order.get("quantity")) or 0
    filled = _as_float(order.get("filledQuantity")) or 0
    held = _as_float(order.get("holdQuantity")) or 0
    return {
        "order_hash": order.get("orderHash"),
        "token_id": order.get("tokenId"),
        "side": order.get("side"),
        "price": _as_float(order.get("price")),
        "quantity": quantity,
        "filled_quantity": filled,
        # Size still on the book excludes anything held for an in-flight
        # settlement; omitting holdQuantity overstates what is actually resting.
        "resting_quantity": round(quantity - filled - held, 8),
        "status": order.get("status"),
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
