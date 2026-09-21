#!/usr/bin/env python3
"""One-time Google OAuth consent for Workflow 6 (PRD/TechDoc doc-review).

Workflow 6 runs non-interactively (triggered by n8n), so it cannot show a Google
consent screen at review time. Instead, run this script ONCE as an authorised
member of the organisation that owns the BRD/PRD docs. It opens the Google
consent screen, you approve Drive read-only access, and it prints (and optionally
saves) an *offline refresh token*. Configure that token via GOOGLE_OAUTH_* and
the app reads org-restricted Google Docs as you, automatically refreshing the
short-lived access token from then on.

Prerequisites (Google Cloud console — see docs/google_oauth_setup.md):
  1. A project with the Google Drive API enabled.
  2. An OAuth 2.0 Client ID of type "Desktop app". Download its client-secret
     JSON.
  3. Your account on the app's OAuth consent screen test users (or the app
     published) and a member of the org that can open the docs.

Usage:
    python scripts/google_oauth_setup.py --client-secret /path/to/client_secret.json
    python scripts/google_oauth_setup.py --client-secret cs.json --out /run/secrets/wf6-gdocs-oauth.json

Then set in the environment (see .env):
    GOOGLE_OAUTH_CLIENT_ID=<client_id from the JSON>
    GOOGLE_OAUTH_CLIENT_SECRET=<client_secret from the JSON>
    GOOGLE_OAUTH_TOKEN_FILE=/run/secrets/wf6-gdocs-oauth.json   # or GOOGLE_OAUTH_REFRESH_TOKEN=<token>
"""

from __future__ import annotations

import argparse
import json
import sys

# Must match app/doc_review.py GOOGLE_SCOPES.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--client-secret",
        default="",
        help="Path to the OAuth client-secret JSON downloaded from Google Cloud "
        "console (Desktop app client). If omitted, GOOGLE_OAUTH_CLIENT_ID and "
        "GOOGLE_OAUTH_CLIENT_SECRET are read from the environment / .env instead.",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Optional path to write a JSON file holding the refresh token "
        "(point GOOGLE_OAUTH_TOKEN_FILE at it). If omitted, the token is only "
        "printed.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open a browser. The auth URL is printed instead; open it "
        "in any browser. On a headless server first forward the callback port, "
        "e.g.  ssh -L 8765:localhost:8765 user@server  then open the URL locally.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Fixed localhost callback port (default: a random free port). Use a "
        "fixed port with --no-browser so you can SSH-forward it.",
    )
    args = parser.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "google-auth-oauthlib is required for this one-time setup.\n"
            "Install it with:  pip install google-auth-oauthlib",
            file=sys.stderr,
        )
        return 2

    if args.client_secret:
        flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, scopes=SCOPES)
    else:
        # No JSON file given — build the client config from the env vars the app
        # already uses (load .env first if python-dotenv is around).
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        import os

        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        if not (client_id and client_secret):
            print(
                "Provide --client-secret <client_secret.json>, or set "
                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in the "
                "environment / .env.",
                file=sys.stderr,
            )
            return 2
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh_token is returned.
    # run_local_server is the only supported InstalledAppFlow path now (run_console
    # and the OOB copy/paste flow were removed/disabled by Google). --no-browser
    # just skips auto-opening; you open the printed URL yourself (forward the port
    # first on a headless host).
    port = args.port if args.port else (8765 if args.no_browser else 0)
    creds = flow.run_local_server(
        port=port,
        open_browser=not args.no_browser,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Open this URL in a browser to authorize:\n\n{url}\n",
    )

    if not creds.refresh_token:
        print(
            "ERROR: Google did not return a refresh token. Revoke prior access at "
            "https://myaccount.google.com/permissions and retry (the flow already "
            "forces prompt=consent).",
            file=sys.stderr,
        )
        return 1

    print("\n=== Google OAuth setup complete ===")
    print(f"GOOGLE_OAUTH_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")

    if args.out:
        payload = {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "scopes": SCOPES,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nWrote refresh token to {args.out}")
        print(f"Set:  GOOGLE_OAUTH_TOKEN_FILE={args.out}")
        print("      GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET (above)")
    else:
        print("\nAdd the three values above to the app environment (.env).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
