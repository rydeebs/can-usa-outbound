"""
agent/auth.py — One-time authentication script.

Run this ONCE on any machine with a browser available:
    cd agent && python auth.py

It prints a URL and a code. Open the URL, enter the code, sign in as Pawel.
The script then saves a token cache file (token_cache.json) that the agent
uses to silently refresh Pawel's access token every time it polls.

Re-run this script if the token expires (after 90 days of no activity)
or if you see authentication errors in the Railway logs.
"""

import json
import os
import sys
from pathlib import Path

import msal
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID")
TENANT_ID     = os.environ.get("AZURE_TENANT_ID", "common")
TOKEN_FILE    = Path(__file__).parent / "token_cache.json"

SCOPES = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/User.Read",
    "offline_access",
]

def main():
    if not CLIENT_ID:
        print("ERROR: AZURE_CLIENT_ID not set in .env")
        sys.exit(1)

    # Load existing cache if present
    cache = msal.SerializableTokenCache()
    if TOKEN_FILE.exists():
        cache.deserialize(TOKEN_FILE.read_text())

    app = msal.PublicClientApplication(
        client_id=CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )

    # Check if we already have a valid token
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            print(f"\n✅ Already authenticated as: {accounts[0].get('username')}")
            print("Token is valid. The agent will use this automatically.")
            _save_cache(cache)
            return

    # Start device code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("ERROR: Could not initiate device flow:", flow.get("error_description"))
        sys.exit(1)

    print("\n" + "="*60)
    print("STEP 1: Open this URL in your browser:")
    print(f"\n  {flow['verification_uri']}\n")
    print("STEP 2: Enter this code when prompted:")
    print(f"\n  {flow['user_code']}\n")
    print("STEP 3: Sign in as Pawel (pawel@canusa.com)")
    print("="*60)
    print("\nWaiting for you to sign in...")

    result = app.acquire_token_by_device_flow(flow)  # blocks until signed in

    if "access_token" not in result:
        print("\nERROR: Authentication failed.")
        print(result.get("error_description", "Unknown error"))
        sys.exit(1)

    # Save the token cache
    _save_cache(cache)

    # Show who signed in
    accounts = app.get_accounts()
    username = accounts[0].get("username") if accounts else "unknown"
    print(f"\n✅ Authenticated as: {username}")
    print(f"Token cache saved to: {TOKEN_FILE}")
    print("\nThe agent will now use this token automatically.")
    print("You do not need to run this script again for ~90 days.")


def _save_cache(cache: msal.SerializableTokenCache):
    TOKEN_FILE.write_text(cache.serialize())


if __name__ == "__main__":
    main()