"""
auth.py — One-time Zerodha Kite login helper.
Run this script manually once each trading day to generate a fresh access token.

NOTE: Auth is only needed for ORDER PLACEMENT.
      Chart data now uses Yahoo Finance (FREE) — no auth needed for data!

Usage:
    python auth.py
"""
import webbrowser
import yaml
from kiteconnect import KiteConnect


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def login():
    cfg = load_config()
    kite = KiteConnect(api_key=cfg["kite"]["api_key"])

    login_url = kite.login_url()
    print(f"\n1. Opening Zerodha login in your browser...")
    print(f"   If it doesn't open, go to:\n   {login_url}\n")
    webbrowser.open(login_url)

    request_token = input("2. After login, paste the 'request_token' from the redirected URL: ").strip()

    data = kite.generate_session(request_token, api_secret=cfg["kite"]["api_secret"])
    access_token = data["access_token"]

    token_file = cfg["kite"]["access_token_file"]
    with open(token_file, "w") as f:
        f.write(access_token)

    print(f"\n✅ Access token saved to '{token_file}'")
    print("   You can now run: python main.py\n")
    return access_token


if __name__ == "__main__":
    login()
