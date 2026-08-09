#!/usr/bin/env python3
"""
YouTube OAuth Headless Flow
============================
Since the server is headless (no browser), this script:
1. Starts a local HTTP server on a port
2. Generates the Google OAuth URL (with redirect to localhost)
3. The user opens the URL on THEIR device
4. Google redirects back to localhost — but that won't reach our server
   So instead, we use the manual copy-paste flow (OOB-style).

Actually, the cleanest approach for a remote headless server:
1. Generate the auth URL with redirect_uri=urn:ietf:wg:oauth:2.0:oob
2. User opens URL in their browser, authorizes
3. Google shows a code on screen
4. User pastes the code back here

But Google deprecated OOB. So we use the local server approach with
a twist: we tell the user to SSH-tunnel or just paste the redirect URL.

Best approach: Use run_local_server with a FIXED port, and tell the
user to add the redirect URI to their Google Cloud Console, then
after authorizing, copy the full redirect URL from the browser
(which will fail to load) and paste it here.
"""
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path("/opt/baal-agent/workspace/vox-director")
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / ".youtube_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

# Use a fixed port so we can tell the user the exact redirect URI
OAUTH_PORT = 9210


def generate_auth_url():
    """Generate the OAuth authorization URL."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        SCOPES,
        redirect_uri=f"http://localhost:{OAUTH_PORT}",
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    return flow, auth_url


def run_headless_auth():
    """Run the OAuth flow for a headless server.

    Two options:
    A) If the user can reach port 9210 (via SSH tunnel or direct IPv6), 
       we start a local server and they get redirected automatically.
    B) Otherwise, we print the auth URL, user authorizes, Google redirects
       to localhost:9210 (which fails on their machine), and they copy
       the full URL from the address bar and paste it here.
    """
    if not CLIENT_SECRET_FILE.exists():
        print("ERROR: client_secret.json not found!")
        sys.exit(1)

    flow, auth_url = generate_auth_url()

    print("=" * 60)
    print("  YouTube OAuth Authorization")
    print("=" * 60)
    print()
    print("Step 1: Open this URL in your browser:")
    print()
    print(auth_url)
    print()
    print("Step 2: Authorize YouTube access for your Google account")
    print()
    print("Step 3: After authorizing, your browser will try to redirect")
    print(f"  to http://localhost:{OAUTH_PORT}/?code=...")
    print()
    print("  If the page fails to load, that's OK!")
    print("  Just copy the FULL URL from your browser's address bar")
    print("  and paste it below.")
    print()
    print("-" * 60)
    redirect_url = input("Paste the redirect URL here: ").strip()

    if not redirect_url:
        print("ERROR: No URL provided")
        sys.exit(1)

    # Parse the code from the redirect URL
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)

    if "code" not in params:
        print("ERROR: No authorization code found in URL")
        print(f"  URL was: {redirect_url[:100]}...")
        sys.exit(1)

    code = params["code"][0]
    print(f"\n  Got authorization code: {code[:20]}...")
    print("  Exchanging for tokens...")

    # Exchange code for credentials
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Save token
    token_data = json.loads(creds.to_json())
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    os.chmod(TOKEN_FILE, 0o600)

    print()
    print(f"✅ YouTube token saved to {TOKEN_FILE}")
    print("  You can now upload videos automatically!")
    print()
    print("  Token will auto-refresh when it expires.")
    return True


def run_local_server_auth():
    """Alternative: Start a local HTTP server and wait for the redirect.
    
    This works if the user can reach port 9210 on the server (direct IPv6 or SSH tunnel).
    We expose it temporarily via Caddy so the user can reach it from the public URL.
    """
    if not CLIENT_SECRET_FILE.exists():
        print("ERROR: client_secret.json not found!")
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        SCOPES,
        redirect_uri=f"http://localhost:{OAUTH_PORT}",
    )

    # Start local server to catch the redirect
    print(f"Starting local OAuth server on port {OAUTH_PORT}...")
    print()
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("Open this URL in your browser:")
    print()
    print(auth_url)
    print()
    print("Waiting for authorization...")

    creds = flow.run_local_server(port=OAUTH_PORT, access_type="offline", prompt="consent", open_browser=False)

    # Save token
    token_data = json.loads(creds.to_json())
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    os.chmod(TOKEN_FILE, 0o600)

    print()
    print(f"✅ YouTube token saved to {TOKEN_FILE}")
    return True


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "manual"

    if mode == "--server":
        run_local_server_auth()
    else:
        run_headless_auth()
