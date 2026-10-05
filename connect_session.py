"""Copy only the Strava session cookie from a local browser into one profile."""
import argparse
import json
import os
from pathlib import Path

import browser_cookie3


def main():
    parser = argparse.ArgumentParser(description="Connect a local Strava browser session")
    parser.add_argument("--profile", default="default",
                        help="profile name: letters, digits, dot, dash, underscore")
    parser.add_argument("--browser", choices=("brave", "chromium"), default="brave")
    parser.add_argument("--cookie-file", type=Path,
                        help="explicit browser Cookies database")
    parser.add_argument("--credentials-dir", type=Path,
                        help="directory for profile JSON files")
    args = parser.parse_args()
    if not args.profile or ".." in args.profile or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for char in args.profile):
        raise SystemExit("Invalid profile name.")

    default_files = {
        "brave": Path.home() / ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        "chromium": Path.home() / ".config/chromium/Default/Cookies",
    }
    cookie_file = args.cookie_file or default_files[args.browser]
    loader = getattr(browser_cookie3, args.browser)
    cookies = loader(cookie_file=str(cookie_file), domain_name="strava.com")
    values = [cookie.value for cookie in cookies
              if cookie.name == "_strava4_session"
              and cookie.domain in (".strava.com", "strava.com", "www.strava.com")]
    if not values:
        raise SystemExit("No Strava session found in the selected browser profile.")

    credentials_dir = args.credentials_dir or Path(
        os.environ.get("STRAVA_CREDENTIALS_DIR", Path(__file__).parent / "profiles"))
    credentials_dir = credentials_dir.expanduser()
    credentials_dir.mkdir(parents=True, exist_ok=True)
    path = credentials_dir / (args.profile + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"session_cookie": values[0]}, handle)
    print(f"Strava session saved as profile '{args.profile}' with mode 600; value not displayed.")


if __name__ == "__main__":
    main()
