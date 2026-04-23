"""
agent/auth.py — One-time Google OAuth authentication.

Run this ONCE on any machine with a browser available:
    cd agent && python auth.py

Opens a browser tab where Pawel signs in with pawel@canusany.com.
Saves credentials to token_google.json which the agent uses to access Gmail.

Re-run if you see authentication errors in Railway logs.

Setup steps before running this:
    1. Go to console.cloud.google.com
    2. Create/select a project
    3. Enable the Gmail API
    4. APIs & Services > Credentials > Create OAuth 2.0 Client ID > Desktop app
    5. Download the JSON > save as agent/google_credentials.json
    6. pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
    7. python auth.py
"""

import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CREDS_FILE = Path(__file__).parent / "google_credentials.json"
TOKEN_FILE = Path(__file__).parent / "token_google.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("ERROR: Google libraries not installed.")
        print("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
        sys.exit(1)

    if not CREDS_FILE.exists():
        print(f"\nERROR: {CREDS_FILE} not found.")
        print("\nTo get this file:")
        print("1. Go to console.cloud.google.com")
        print("2. Create a project (e.g. 'CAN USA Agent')")
        print("3. Enable Gmail API: APIs & Services > Enable APIs > search Gmail > Enable")
        print("4. APIs & Services > Credentials > + Create Credentials > OAuth 2.0 Client ID")
        print("5. Application type: Desktop app > Name: CAN USA Agent > Create")
        print("6. Download JSON > save as agent/google_credentials.json")
        sys.exit(1)

    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            print("\n✅ Token refreshed successfully.")
            return
        except Exception as e:
            print(f"Refresh failed: {e} — re-authenticating...")
            creds = None

    # Already valid
    if creds and creds.valid:
        print("\n✅ Already authenticated — token is valid.")
        return

    # Run OAuth flow — opens browser
    print("\n" + "="*60)
    print("A browser tab will open for Google sign-in.")
    print("Sign in as: pawel@canusany.com")
    print("Grant all permissions when prompted.")
    print("="*60 + "\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
    )

    TOKEN_FILE.write_text(creds.to_json())
    print(f"\n✅ Authenticated successfully.")
    print(f"Token saved to: {TOKEN_FILE}")
    print("\nThe agent will use this token automatically.")
    print("It refreshes itself — no action needed unless you revoke access.")


if __name__ == "__main__":
    main()