"""Public surface the Starchild loader registers.

The loader auto-discovers this module and exposes every public function in it,
so this file stays a thin re-export list. Implementation lives in the modules
alongside it.
"""

from .auth import strix_account, strix_connect, strix_disconnect
from .markets import (
    strix_find_submarket,
    strix_leaderboard,
    strix_market,
    strix_orderbook,
    strix_quote,
    strix_search_markets,
)
from .trading import (
    strix_buy,
    strix_cancel_all,
    strix_claimable,
    strix_limit_order,
    strix_open_orders,
    strix_positions,
    strix_redeem,
    strix_sell,
    strix_status,
)

__all__ = [
    # Account
    "strix_connect",
    "strix_disconnect",
    "strix_account",
    # Market data — no credentials needed
    "strix_search_markets",
    "strix_market",
    "strix_find_submarket",
    "strix_quote",
    "strix_orderbook",
    "strix_leaderboard",
    # Trading
    "strix_buy",
    "strix_sell",
    "strix_limit_order",
    "strix_cancel_all",
    "strix_open_orders",
    # Portfolio
    "strix_status",
    "strix_positions",
    "strix_claimable",
    "strix_redeem",
]
