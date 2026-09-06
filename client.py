"""HTTP client and HMAC request signing for the Strix REST API.

This is a Python port of `strix-sdk`'s TypeScript `HttpClient`. The signing
scheme has to match the venue byte for byte, so three details are load-bearing
and are each covered by a test:

1. The signed message is ``timestamp + METHOD + path + body``, where ``path``
   is the path *after* the ``/api`` mount plus its query string. Signing the
   full request URL produces a valid-looking signature the server rejects.
2. The request body must be the exact bytes that were signed. Python's default
   ``json.dumps`` inserts spaces and escapes non-ASCII, neither of which
   ``JSON.stringify`` does, so both settings are pinned in ``encode_body``.
3. Query strings use form encoding (space as ``+``), matching JS
   ``URLSearchParams``. ``urlencode`` agrees with it on every case tested.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol

DEFAULT_TIMEOUT = 10.0

BASE_URLS = {
    "staging": "https://api-staging.strixlab.io/api",
    "prod": "https://api.strixlab.io/api",
}


class StrixError(Exception):
    """Base class for every error this client raises."""


class StrixConfigError(StrixError):
    """Configuration is missing or malformed — raised before any network call."""


class StrixAuthError(StrixError):
    """An authenticated call was attempted without credentials."""

    def __init__(self, method_name: str) -> None:
        super().__init__(
            f"{method_name!r} requires API credentials. Connect first with "
            "strix_connect(api_key, api_secret, api_passphrase)."
        )


class StrixApiError(StrixError):
    """The venue returned an error response."""

    def __init__(
        self,
        message: str,
        status: int,
        endpoint: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.retry_after = retry_after


def format_price(price: float | None) -> str:
    """Render a probability as a percentage without losing tick precision.

    ``0.625`` on a 0.001-tick market is 62.5%, not 62% — and this string is
    what an agent relays when it asks the user to confirm a trade.
    """
    if price is None:
        return "—"
    percent = price * 100
    if abs(percent - round(percent)) < 1e-9:
        return f"{round(percent):g}%"
    return f"{percent:.4g}%"


def build_signature(
    secret: str, timestamp: str, method: str, path: str, body: str = ""
) -> str:
    """HMAC-SHA256 of ``timestamp + METHOD + path + body``, base64-encoded.

    ``path`` includes the query string and excludes the ``/api`` mount prefix.
    """
    message = f"{timestamp}{method.upper()}{path}{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_ws_signature(secret: str, timestamp: str) -> str:
    """Signature for the WebSocket auth frame — a fixed ``GET /ws``."""
    return build_signature(secret, timestamp, "GET", "/ws")


def build_timestamp() -> str:
    """Current Unix time in seconds, as the string the venue expects."""
    return str(int(time.time()))


# Above this, JS switches to exponential notation (1e21 prints as "1e+21"),
# so an integral float is only safely an integer below it.
_JS_MAX_PLAIN_INTEGER = 10**21


def _js_numbers(value: Any) -> Any:
    """Rewrite floats the way JavaScript renders them.

    Every JS number is a double, and ``JSON.stringify`` prints the shortest
    representation — so ``15.0`` becomes ``15``, with no trailing ``.0``.
    Python prints ``15.0``, and the venue re-serialises the body it parsed
    before checking the signature, so an integral float signs one byte string
    and is verified against another. The result is a bare "Invalid signature"
    on every order with a whole-number quantity.
    """
    if isinstance(value, bool):
        # bool is an int subclass; must be tested first or True becomes 1.
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(
                f"Cannot send {value!r} in a JSON body — it has no JSON representation."
            )
        if value.is_integer() and abs(value) < _JS_MAX_PLAIN_INTEGER:
            return int(value)
        return value
    if isinstance(value, dict):
        return {key: _js_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_js_numbers(item) for item in value]
    return value


def encode_body(body: Any) -> str:
    """Serialise a request body exactly as JS ``JSON.stringify`` would.

    Three things have to match, because the venue re-serialises the body it
    parsed before verifying the signature:

    - ``separators`` drops the spaces Python inserts by default;
    - ``ensure_ascii=False`` keeps non-ASCII characters literal;
    - :func:`_js_numbers` renders integral floats without a trailing ``.0``.
    """
    if body is None:
        return ""
    return json.dumps(_js_numbers(body), separators=(",", ":"), ensure_ascii=False)


def build_query_string(params: Mapping[str, Any] | None) -> str:
    """Form-encode query params, dropping ``None`` values.

    Mirrors JS ``URLSearchParams``: values are stringified, and ``None`` is
    omitted rather than sent as the literal ``"None"``. Booleans are lowercased
    to match JS ``String(true)``.
    """
    if not params:
        return ""
    pairs = [
        (key, _stringify_param(value))
        for key, value in params.items()
        if value is not None
    ]
    if not pairs:
        return ""
    return "?" + urllib.parse.urlencode(pairs)


def _stringify_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Transport(Protocol):
    """The seam tests substitute for the network."""

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str,
        timeout: float,
    ) -> tuple[int, str]:
        """Return ``(status_code, response_text)``."""
        ...


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: str,
    timeout: float,
) -> tuple[int, str]:
    """Default transport — stdlib only, so the skill has no pip dependencies."""
    data = body.encode("utf-8") if body else None
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # An error response still carries a body, and the venue puts its
        # explanation there — surface it rather than the bare status.
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise StrixApiError(f"Network error: {exc.reason}", 0, f"{method} {url}") from exc
    except TimeoutError as exc:
        raise StrixApiError(
            f"Request timed out after {timeout}s", 408, f"{method} {url}"
        ) from exc


def resolve_base_url(env_or_url: str) -> str:
    """Accept either an environment name (``staging``/``prod``) or a full URL."""
    # Strip first and use the stripped value: a trailing newline from a shell
    # `export` would otherwise end up inside the base URL.
    cleaned = env_or_url.strip()
    key = cleaned.lower()
    if key in BASE_URLS:
        return BASE_URLS[key]
    if key.startswith("http://") or key.startswith("https://"):
        return cleaned.rstrip("/")
    raise StrixConfigError(
        f"Unknown Strix environment {env_or_url!r}. "
        f"Use one of {sorted(BASE_URLS)} or a full https:// base URL."
    )


class StrixClient:
    """Minimal REST client for the Strix API.

    Credentials are optional: public market data needs none, and every
    authenticated method raises :class:`StrixAuthError` when they are absent.
    """

    def __init__(
        self,
        base_url: str = "staging",
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = resolve_base_url(base_url)
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.timeout = timeout
        self._transport: Transport = transport or urllib_transport

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    def require_credentials(self, method_name: str) -> None:
        if not self.has_credentials:
            raise StrixAuthError(method_name)

    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        authenticated: bool = False,
    ) -> Any:
        """Send one request and return the unwrapped ``data`` payload.

        Every venue response is an envelope — ``{"success": bool, "data": ...}``
        — so callers get the payload rather than having to unwrap it themselves.
        """
        if authenticated:
            self.require_credentials(f"{method} {path}")

        path_with_qs = path + build_query_string(params)
        url = self.base_url + path_with_qs
        body_str = encode_body(body)

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.has_credentials:
            headers.update(self._auth_headers(method, path_with_qs, body_str))

        status, text = self._transport(
            method.upper(), url, headers, body_str, self.timeout
        )
        return self._unwrap(status, text, f"{method.upper()} {path}")

    def _auth_headers(
        self, method: str, path_with_qs: str, body_str: str
    ) -> dict[str, str]:
        timestamp = build_timestamp()
        assert self.api_secret is not None  # guarded by has_credentials
        return {
            "STRIX-API-KEY": self.api_key or "",
            "STRIX-SIGNATURE": build_signature(
                self.api_secret, timestamp, method, path_with_qs, body_str
            ),
            "STRIX-TIMESTAMP": timestamp,
            "STRIX-PASSPHRASE": self.api_passphrase or "",
        }

    def _unwrap(self, status: int, text: str, endpoint: str) -> Any:
        if not text.strip():
            payload = {}
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                # A body we cannot parse is not an empty result. Treating it as
                # one would report "0 open orders" or a successful cancel-all
                # that never ran.
                if status >= 400:
                    raise StrixApiError(f"HTTP {status}", status, endpoint) from None
                raise StrixApiError(
                    "The venue returned a response that is not JSON "
                    f"(HTTP {status}). It may be an outage or a proxy error page.",
                    status,
                    endpoint,
                ) from None

        message = ""
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error") or ""

        if status >= 400:
            raise StrixApiError(message or f"HTTP {status}", status, endpoint)

        # A 200 carrying `success: false` is still a refusal. Returning its
        # payload would report a redemption or an order that never happened.
        if isinstance(payload, dict) and payload.get("success") is False:
            raise StrixApiError(
                message or "The venue rejected this request.", status, endpoint
            )

        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def server_time(self) -> int:
        """Venue clock in Unix seconds. Signatures fail past 30s of drift."""
        result = self.request("GET", "/time")
        if isinstance(result, dict):
            return int(result.get("serverTime", 0))
        return int(result)

    def clock_drift(self) -> int:
        """Absolute difference in seconds between local and venue clocks."""
        return abs(self.server_time() - int(time.time()))
