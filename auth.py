"""Connecting an existing Strix account to the agent.

The agent never creates an account, provisions a wallet, or signs anything on
chain. A human generates an API key in the Strix web app (Settings → API Keys),
and this module verifies and stores it. Everything else in the skill runs on
that credential.
"""

from __future__ import annotations

from .client import StrixApiError, StrixError
from .session import (
    ENV_API_KEY,
    ENV_API_PASSPHRASE,
    ENV_API_SECRET,
    clear_credentials,
    current_environment,
    environment_credentials,
    mask,
    resolve_client,
    save_credentials,
    stored_credentials,
)


def strix_connect(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    environment: str = "",
) -> dict:
    """Verify a Strix API credential and remember it for later calls.

    Returns a dict describing the account. The secret and passphrase are never
    echoed back — only a masked form of the key — so the result is safe to show
    in a transcript.
    """
    if not (api_key and api_secret and api_passphrase):
        return {
            "connected": False,
            "error": (
                "All three of api_key, api_secret and api_passphrase are required. "
                "Generate them in the Strix app under Settings → API Keys."
            ),
        }

    env = environment or current_environment()
    client = resolve_client(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        environment=env,
    )

    try:
        identity = client.request("GET", "/auth/whoami", authenticated=True)
    except StrixApiError as exc:
        if exc.status in (401, 403):
            detail = "Strix rejected these credentials. Check they were copied in full and have not been revoked."
        elif exc.status == 404:
            detail = (
                "This Strix deployment does not support credential identification "
                "(needs /auth/whoami). Check the environment setting."
            )
        else:
            detail = f"{exc} (HTTP {exc.status})"
        return {"connected": False, "error": detail}
    except StrixError as exc:
        return {"connected": False, "error": str(exc)}

    from_environment = environment_credentials()
    save_credentials(api_key, api_secret, api_passphrase, env)

    result = {
        "connected": True,
        "environment": env,
        "api_key": mask(api_key),
        "user_id": identity.get("userId"),
        "wallet_address": identity.get("walletAddress"),
        "username": identity.get("username"),
        "summary": (
            f"Connected to Strix ({env}) as "
            f"{identity.get('username') or identity.get('walletAddress')}."
        ),
        "note": (
            "Deposit USDC to the wallet address above — but only on the chain "
            "this deployment settles on. The right token on the wrong chain is "
            "not recoverable."
        ),
    }

    if from_environment and from_environment.get("apiKey") != api_key:
        result["shadowed_environment_credentials"] = True
        result["warning"] = (
            f"A different credential is also set in {ENV_API_KEY} "
            f"({mask(from_environment.get('apiKey'))}). The one just connected "
            "takes precedence. Unset the environment variables, or run "
            "strix_disconnect, to go back to using it."
        )

    return result


def strix_disconnect() -> dict:
    """Forget the stored credential in this container.

    This does not revoke the key at Strix. To revoke it, use Settings → API
    Keys in the web app.

    If credentials are also set as environment variables, clearing the stored
    one falls back to them rather than leaving the agent unable to trade — so
    this reports that plainly instead of implying it is now detached.
    """
    had_credential = clear_credentials()
    remaining = environment_credentials()

    note = (
        "The credential was removed from this agent only. It is still valid at "
        "Strix — revoke it in Settings → API Keys if that was the intent."
    )
    if remaining:
        note = (
            "The stored credential was removed, but this agent can STILL TRADE: "
            f"credentials are also set in {ENV_API_KEY}, {ENV_API_SECRET} and "
            f"{ENV_API_PASSPHRASE} ({mask(remaining.get('apiKey'))}), and those "
            "are now in use. Unset those variables to fully disconnect."
        )

    return {
        "disconnected": not remaining,
        "had_stored_credential": had_credential,
        "still_connected_via_environment": bool(remaining),
        "note": note,
    }


def strix_account() -> dict:
    """Which Strix account this agent is currently acting as."""
    stored = stored_credentials()
    if not stored.get("apiKey"):
        return {
            "connected": False,
            "error": "No Strix credential configured. Run strix_connect first.",
        }

    client = resolve_client()
    identity = client.request("GET", "/auth/whoami", authenticated=True) or {}
    result = {
        "connected": True,
        "environment": stored.get("environment"),
        "api_key": mask(stored.get("apiKey")),
        "user_id": identity.get("userId"),
        "wallet_address": identity.get("walletAddress"),
        "username": identity.get("username"),
    }

    # Someone who sets the environment variables after connecting would
    # otherwise keep trading on the stored account with nothing to say so.
    from_environment = environment_credentials()
    if from_environment and from_environment.get("apiKey") != stored.get("apiKey"):
        result["shadowed_environment_credentials"] = True
        result["warning"] = (
            f"A different credential is set in {ENV_API_KEY} "
            f"({mask(from_environment.get('apiKey'))}) but is NOT in use — the "
            "stored one above takes precedence. Run strix_disconnect to switch "
            "to the environment credential."
        )

    return result
