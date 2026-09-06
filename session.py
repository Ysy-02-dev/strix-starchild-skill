"""Where the agent's Strix credentials live between calls.

Two sources, checked in order:

1. Environment variables — ``STRIX_API_KEY`` / ``STRIX_API_SECRET`` /
   ``STRIX_API_PASSPHRASE``. This is the path to prefer, and the one the
   ``SKILL.md`` documents first: the secret never passes through a chat
   transcript.
2. A state file written by ``strix_connect``, for agents that have no way to
   set environment variables.

A credential written by ``strix_connect`` takes precedence over the
environment: connect verifies a specific account before storing it, and an
ambient variable must not silently redirect later orders somewhere else.

The state file is deliberately plain JSON with owner-only permissions rather
than encrypted. A Starchild agent runs in a per-user sandboxed container, so
the isolation boundary is the container itself; an encryption key stored beside
the ciphertext inside that same container would add ceremony, not secrecy. This
is the one place the reasoning differs from the Telegram bot, which encrypts
because it is a multi-tenant server holding many users' credentials in one
database.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .client import StrixClient

ENV_API_KEY = "STRIX_API_KEY"
ENV_API_SECRET = "STRIX_API_SECRET"
ENV_API_PASSPHRASE = "STRIX_API_PASSPHRASE"
ENV_ENVIRONMENT = "STRIX_ENV"
ENV_STATE_PATH = "STRIX_STATE_PATH"

DEFAULT_ENVIRONMENT = "staging"


def state_path() -> Path:
    """Where connect-time credentials are cached."""
    override = os.environ.get(ENV_STATE_PATH)
    if override:
        return Path(override)
    return Path.home() / ".strix" / "credentials.json"


def _read_state() -> dict:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_credentials(
    api_key: str, api_secret: str, api_passphrase: str, environment: str
) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "apiKey": api_key,
                "apiSecret": api_secret,
                "apiPassphrase": api_passphrase,
                "environment": environment,
            }
        ),
        encoding="utf-8",
    )
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Best effort — some filesystems (and Windows) ignore POSIX modes.
        pass


def clear_credentials() -> bool:
    """Forget the cached credential. Returns whether there was one."""
    path = state_path()
    if path.is_file():
        path.unlink()
        return True
    return False


def environment_credentials() -> dict:
    """Credentials configured as environment variables, if all three are set."""
    env_key = os.environ.get(ENV_API_KEY)
    env_secret = os.environ.get(ENV_API_SECRET)
    env_passphrase = os.environ.get(ENV_API_PASSPHRASE)
    if env_key and env_secret and env_passphrase:
        return {
            "apiKey": env_key,
            "apiSecret": env_secret,
            "apiPassphrase": env_passphrase,
            "environment": os.environ.get(ENV_ENVIRONMENT, DEFAULT_ENVIRONMENT),
        }
    return {}


def stored_credentials() -> dict:
    """The credential this agent will actually trade with.

    A credential saved by ``strix_connect`` wins over the environment. Connect
    is a deliberate act against a specific account, and it verifies that
    account before storing; letting an ambient environment variable silently
    redirect later orders to a different account is the kind of mismatch that
    is only noticed after a trade.
    """
    from_file = _read_state()
    if from_file.get("apiKey"):
        return from_file
    return environment_credentials()


def current_environment() -> str:
    stored = stored_credentials()
    if stored.get("environment"):
        return stored["environment"]
    return os.environ.get(ENV_ENVIRONMENT, DEFAULT_ENVIRONMENT)


def resolve_client(require_credentials: bool = False, **overrides) -> StrixClient:
    """Build a client from whatever credentials are configured.

    Passing ``require_credentials`` does not raise here — the client raises
    :class:`StrixAuthError` at call time, so the error names the method the
    agent actually invoked.
    """
    stored = stored_credentials()
    return StrixClient(
        base_url=overrides.get("environment")
        or stored.get("environment")
        or DEFAULT_ENVIRONMENT,
        api_key=overrides.get("api_key") or stored.get("apiKey"),
        api_secret=overrides.get("api_secret") or stored.get("apiSecret"),
        api_passphrase=overrides.get("api_passphrase") or stored.get("apiPassphrase"),
    )


def mask(secret: str | None, keep: int = 4) -> str:
    """Render a credential safe to print: first 4 characters and last 4."""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "…"
    return f"{secret[:keep]}…{secret[-keep:]}"
