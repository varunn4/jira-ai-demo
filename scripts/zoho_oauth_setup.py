#!/usr/bin/env python3
"""One-time Zoho Desk OAuth consent for the "Zoho Tickets" admin tab.

The tab calls Zoho Desk's *search* endpoints (map customer email/phone -> contact,
then list that contact's tickets). Those endpoints need the ``Desk.search.READ``
scope IN ADDITION to ``Desk.contacts.READ`` / ``Desk.tickets.READ``. Scopes are
baked into a refresh token when it is granted — you cannot add a scope to an
existing token, so if you see::

    403 SCOPE_MISMATCH — The OAuth Token does not contain the scope ...

you must mint a NEW refresh token with all the scopes below. This script does the
authorization-code -> refresh-token exchange for you.

Scopes requested (edit SCOPE below if you need fewer/more):
    Desk.tickets.READ    — read tickets + a contact's tickets
    Desk.contacts.READ   — read contact records
    Desk.search.READ     — /contacts/search and /tickets/search   ← the missing one

Flow (Zoho self-client / server-based app):
  1. Go to https://api-console.zoho.in -> your client -> "Generate Code"
     (Self Client tab), OR build the consent URL this script prints and open it
     in a browser, approving access. Either way you get a one-time *grant code*.
  2. Run this script with that code; it exchanges it for a refresh token and
     prints it. Copy the value into ZOHO_REFRESH_TOKEN in .env.

Usage:
    # Just print the consent URL to open in a browser:
    python scripts/zoho_oauth_setup.py --print-auth-url

    # Exchange a grant code (from the console or the consent redirect) for a token:
    python scripts/zoho_oauth_setup.py --code 1000.abc123...

Credentials are read from the environment (ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET,
ZOHO_REDIRECT_URI, ZOHO_ACCOUNTS_BASE) or can be passed as flags.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlencode

import requests

SCOPE = "Desk.tickets.READ,Desk.contacts.READ,Desk.search.READ"


def _accounts_base() -> str:
    return os.getenv("ZOHO_ACCOUNTS_BASE", "https://accounts.zoho.in").rstrip("/")


def build_auth_url(client_id: str, redirect_uri: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "access_type": "offline",   # required to receive a refresh token
        "prompt": "consent",         # force a fresh refresh token every time
    }
    return f"{_accounts_base()}/oauth/v2/auth?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    params = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }
    # Self-Client-generated codes have no redirect; only send it when set (the
    # server-based / redirect flow). A mismatched redirect_uri => invalid_code.
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    resp = requests.post(f"{_accounts_base()}/oauth/v2/token", params=params, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    if resp.status_code != 200 or "refresh_token" not in data:
        print(f"Token exchange failed ({resp.status_code}): {data}", file=sys.stderr)
        sys.exit(1)
    return data


def _load_env() -> None:
    """Load ZOHO_* from the repo-root .env when run standalone (no app runtime).

    Uses python-dotenv if available, else a minimal manual parser so it works
    from any venv. Only fills vars that are not already set in the environment.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, "..", ".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except ImportError:
        pass
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val


def main() -> int:
    _load_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-id", default=os.getenv("ZOHO_CLIENT_ID", ""))
    p.add_argument("--client-secret", default=os.getenv("ZOHO_CLIENT_SECRET", ""))
    p.add_argument("--redirect-uri", default=os.getenv("ZOHO_REDIRECT_URI", ""))
    p.add_argument("--code", help="One-time grant code from the API console / consent redirect")
    p.add_argument("--print-auth-url", action="store_true", help="Print the consent URL and exit")
    args = p.parse_args()

    if not args.client_id or not args.client_secret:
        print("ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET are required (env or flags).", file=sys.stderr)
        return 2

    if args.print_auth_url:
        if not args.redirect_uri:
            print("--redirect-uri (or ZOHO_REDIRECT_URI) is required to build the auth URL.", file=sys.stderr)
            return 2
        print("Open this URL in a browser, approve access, then copy the ?code=... value:\n")
        print(build_auth_url(args.client_id, args.redirect_uri))
        print("\nAlternatively use api-console.zoho.in -> Self Client -> Generate Code with scope:\n  " + SCOPE)
        return 0

    code = args.code
    if not code:
        # Interactive so you can generate a FRESH code and paste it immediately —
        # codes are single-use and expire in ~5 min, so never reuse an old one.
        print("Client ID being used:", args.client_id or "(none)")
        print("\nNow go to the Zoho console -> Self Client -> Generate Code,")
        print("click CREATE, then paste the fresh code here.")
        code = input("\nGrant code: ").strip()
    if not code:
        print("No code provided.", file=sys.stderr)
        return 2

    data = exchange_code(args.client_id, args.client_secret, args.redirect_uri, code)
    print("\n✅ Success. Set this in .env:\n")
    print(f"ZOHO_REFRESH_TOKEN={data['refresh_token']}")
    print(f"\n(granted scopes: {data.get('scope', SCOPE)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
