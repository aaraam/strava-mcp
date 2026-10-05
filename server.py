"""Read-only, multi-profile Strava MCP adapter."""
import html
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP


class StravaError(Exception):
    pass


PROFILE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def validate_profile(profile):
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile) or ".." in profile:
        raise StravaError("Invalid profile name.")
    return profile


class CredentialStore:
    """Resolve named, mode-600 credential files without exposing their values."""

    def __init__(self, credentials_dir=None, legacy_session_file=None):
        configured_dir = credentials_dir or os.environ.get("STRAVA_CREDENTIALS_DIR")
        self.directory = Path(configured_dir).expanduser() if configured_dir else (
            Path(__file__).parent / "profiles")
        if legacy_session_file is not None:
            self.legacy_session_file = Path(legacy_session_file)
        elif os.environ.get("STRAVA_SESSION_FILE"):
            self.legacy_session_file = Path(os.environ["STRAVA_SESSION_FILE"])
        elif configured_dir:
            self.legacy_session_file = None
        else:
            self.legacy_session_file = Path(__file__).parent / "session.json"

    def path_for(self, profile):
        profile = validate_profile(profile)
        return self.directory / (profile + ".json")

    def _file_for(self, profile):
        profile = validate_profile(profile)
        candidate = self.path_for(profile)
        if profile == "default":
            if os.environ.get("STRAVA_SESSION_FILE"):
                return self.legacy_session_file
            if candidate.exists():
                return candidate
            if self.legacy_session_file is not None and self.legacy_session_file.exists():
                return self.legacy_session_file
        return candidate

    def _read(self, path):
        try:
            if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise StravaError("Credential file must have permissions 600.")
            data = json.loads(path.read_text())
        except StravaError:
            raise
        except (OSError, ValueError):
            raise StravaError("Missing or invalid local Strava credential file.") from None
        if not isinstance(data, dict):
            raise StravaError("Invalid local Strava credential file.")
        token = data.get("access_token")
        cookie = data.get("session_cookie")
        if bool(token) == bool(cookie):
            raise StravaError("Supply exactly one access_token or session_cookie.")
        value = token or cookie
        if not isinstance(value, str) or re.search(r"[\s;\r\n]", value):
            raise StravaError("Invalid session credential format.")
        return token, cookie

    def credentials(self, profile="default"):
        profile = validate_profile(profile)
        path = self._file_for(profile)
        if path.exists():
            return self._read(path)
        if profile == "default":
            token = os.environ.get("STRAVA_ACCESS_TOKEN")
            cookie = os.environ.get("STRAVA_SESSION_COOKIE")
            if bool(token) == bool(cookie) and (token or cookie):
                raise StravaError("Set only one Strava credential environment variable.")
            if token or cookie:
                value = token or cookie
                if re.search(r"[\s;\r\n]", value):
                    raise StravaError("Invalid session credential format.")
                return token, cookie
        raise StravaError("Strava profile is not configured.")

    def list_profiles(self):
        names = set()
        try:
            for path in self.directory.glob("*.json"):
                if path.is_file() and PROFILE_RE.fullmatch(path.stem) and ".." not in path.stem:
                    names.add(path.stem)
        except OSError:
            pass
        if self._file_for("default").exists() or os.environ.get(
                "STRAVA_ACCESS_TOKEN") or os.environ.get("STRAVA_SESSION_COOKIE"):
            names.add("default")
        return sorted(names)

    def status(self, profile="default"):
        profile = validate_profile(profile)
        path = self._file_for(profile)
        if path.exists():
            try:
                token, cookie = self._read(path)
                return {"profile": profile, "configured": True,
                        "credential_kind": "access_token" if token else "session_cookie",
                        "source": "file", "path": str(path)}
            except StravaError as error:
                return {"profile": profile, "configured": False,
                        "credential_kind": None, "source": "file", "path": str(path),
                        "error": str(error)}
        if profile == "default":
            token = os.environ.get("STRAVA_ACCESS_TOKEN")
            cookie = os.environ.get("STRAVA_SESSION_COOKIE")
            if token or cookie:
                return {"profile": profile, "configured": True,
                        "credential_kind": "access_token" if token else "session_cookie",
                        "source": "environment", "path": None}
        return {"profile": profile, "configured": False,
                "credential_kind": None, "source": None, "path": str(path)}


class StravaClient:
    def __init__(self, session_file=None, transport=None, interval=1.0, *,
                 profile="default", credential_store=None):
        self.profile_name = validate_profile(profile)
        if credential_store is not None:
            self.store = credential_store
        else:
            self.store = CredentialStore(legacy_session_file=session_file)
        self.transport = transport
        self.interval = interval
        self.last_request = 0.0
        self.athlete_id = None

    def _session(self):
        return self.store.credentials(self.profile_name)

    def get(self, path, params=None):
        if not re.fullmatch(r"[A-Za-z0-9_/,.-]+", path) or ".." in path:
            raise StravaError("Invalid endpoint.")
        token, cookie = self._session()
        headers = {"Accept": "application/json", "User-Agent": "PersonalStravaMCP/0.1"}
        if token:
            headers["Authorization"] = "Bearer " + token
            host = "cdn-1.strava.com"
        else:
            headers["Cookie"] = "_strava4_session=" + cookie
            host = "www.strava.com"
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            with httpx.Client(transport=self.transport, timeout=30,
                              follow_redirects=False, trust_env=False) as client:
                response = client.get(f"https://{host}/api/v3/{path}",
                                      params=params, headers=headers)
        except httpx.HTTPError:
            raise StravaError("Strava network request failed.") from None
        if response.status_code == 429:
            raise StravaError("Strava rate limit reached; stop and retry later.")
        if response.status_code in (401, 403) or 300 <= response.status_code < 400:
            raise StravaError("Session expired, unsupported, or access denied.")
        if not response.is_success:
            raise StravaError(f"Strava returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise StravaError("Expected JSON; the session may require browser login.") from None

    def _web_get(self, path, params=None):
        if not re.fullmatch(r"/[A-Za-z0-9_/?=&,.-]+", path) or ".." in path:
            raise StravaError("Invalid web endpoint.")
        _, cookie = self._session()
        if not cookie:
            raise StravaError("Web session mode requires a session cookie.")
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            with httpx.Client(transport=self.transport, timeout=30,
                              follow_redirects=False, trust_env=False) as client:
                response = client.get(
                    "https://www.strava.com" + path, params=params,
                    headers={"Cookie": "_strava4_session=" + cookie,
                             "User-Agent": "Mozilla/5.0",
                             "Accept": "text/html,application/json",
                             "X-Requested-With": "XMLHttpRequest"})
        except httpx.HTTPError:
            raise StravaError("Strava web request failed.") from None
        if response.status_code == 429:
            raise StravaError("Strava rate limit reached; stop and retry later.")
        if response.status_code in (401, 403) or 300 <= response.status_code < 400:
            raise StravaError(f"Strava web access returned HTTP {response.status_code}.")
        if not response.is_success:
            raise StravaError(f"Strava web returned HTTP {response.status_code}.")
        return response

    def _parse_activity_page(self, text, activity_id):
        soup = BeautifulSoup(text, "lxml")
        page_match = re.search(r"var pageProps = (\{.*?\});", text, re.S)
        athlete_match = re.search(
            r"new Strava\.Models\.CurrentAthlete\((\{.*?\})\);", text, re.S)
        try:
            page_props = json.loads(html.unescape(page_match.group(1))) if page_match else {}
            current = json.loads(html.unescape(athlete_match.group(1))) if athlete_match else {}
        except (ValueError, TypeError):
            raise StravaError("Strava activity page contained invalid embedded data.") from None
        if page_props.get("activity_id") != activity_id:
            raise StravaError("Activity page identity did not match the requested ID.")
        if not isinstance(current.get("id"), int):
            raise StravaError("Strava page did not identify the authenticated athlete.")
        self.athlete_id = current["id"]
        props = []
        for node in soup.select("[data-react-props]"):
            try:
                value = json.loads(html.unescape(node["data-react-props"]))
                if isinstance(value, dict):
                    props.append(value)
            except (KeyError, ValueError, TypeError):
                continue
        stats = {}
        for item in soup.select("ul.inline-stats li"):
            label = item.select_one(".label")
            value = item.select_one("strong")
            if label and value:
                stats[" ".join(label.get_text(" ", strip=True).split())] = \
                    " ".join(value.get_text(" ", strip=True).split())
        description = None
        for selector in (".activity-description", '[data-testid="activity-description"]'):
            node = soup.select_one(selector)
            if node:
                description = node.get_text(" ", strip=True)
                break
        return {"activity_id": activity_id, "page_props": page_props,
                "current_athlete": current, "stats": stats,
                "description": description, "react_props": props,
                "page_title": soup.title.get_text(" ", strip=True) if soup.title else None}

    def profile(self):
        _, cookie = self._session()
        if cookie:
            listing = self.activities(1)
            if not listing.get("models"):
                raise StravaError("No activity page is available to identify the athlete.")
            page = self._web_get(f"/activities/{listing['models'][0]['id']}")
            return self._parse_activity_page(page.text, listing["models"][0]["id"])["current_athlete"]
        result = self.get("athlete")
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise StravaError("Unexpected athlete response; live adapter needs adjustment.")
        self.athlete_id = result["id"]
        return result

    def self_id(self):
        if self.athlete_id is None:
            self.profile()
        return self.athlete_id

    def activities(self, page=1, query=""):
        if type(page) is not int or page < 1:
            raise StravaError("page must be a positive integer.")
        token, cookie = self._session()
        if cookie:
            if query:
                raise StravaError("Browser-session search is not verified; use pagination without a query.")
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with httpx.Client(transport=self.transport, timeout=30,
                                  follow_redirects=False, trust_env=False) as web:
                    response = web.get('https://www.strava.com/athlete/training_activities',
                        params={'page': page, 'per_page': 20}, headers={
                            'Cookie': '_strava4_session=' + cookie,
                            'User-Agent': 'Mozilla/5.0',
                            'Accept': 'text/html,application/json',
                            'X-Requested-With': 'XMLHttpRequest'})
                if response.status_code != 200:
                    raise StravaError(f"Activity history returned HTTP {response.status_code}.")
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get('models'), list):
                    raise StravaError("Unexpected browser activity response.")
                return result
            except StravaError:
                raise
            except (httpx.HTTPError, ValueError):
                raise StravaError("Browser activity request failed or returned non-JSON.") from None
        self.self_id()
        return self.get("activities/search", {"page": page, "keywords": query,
                                               "commutes": "true"})

    def activity(self, activity_id):
        if type(activity_id) is not int or activity_id <= 0:
            raise StravaError("activity_id must be a positive integer.")
        token, cookie = self._session()
        if cookie:
            result = self._parse_activity_page(
                self._web_get(f"/activities/{activity_id}").text, activity_id)
            owner = self.self_id()
            actual = result["page_props"].get("activity_athlete_id")
            if actual is not None and str(actual) != str(owner):
                raise StravaError("Activity ownership could not be verified as your account.")
            return result
        owner = self.self_id()
        result = self.get(f"activities/{activity_id}")
        if not isinstance(result, dict):
            raise StravaError("Unexpected activity response.")
        athlete = result.get("athlete")
        actual = result.get("athlete_id") or (
            athlete.get("id") if isinstance(athlete, dict) else None)
        if actual is None or str(actual) != str(owner):
            raise StravaError("Activity ownership could not be verified as your account.")
        return result

    def streams(self, activity_id):
        if self._session()[1]:
            raise StravaError("Browser session reaches activity pages, but Strava returned 403 for streams.")
        self.activity(activity_id)
        types = "time,latlng,distance,altitude,heartrate,cadence,watts,temp,moving,velocity_smooth,grade_smooth"
        return self.get(f"activities/{activity_id}/streams/{types}",
                        {"resolution": "high", "series_type": "time"})

    def gear(self):
        if self._session()[1]:
            raise StravaError("Browser session gear endpoint redirected; token mode is required.")
        return self.get(f"athletes/{self.self_id()}/gear")

    def stats(self):
        if self._session()[1]:
            raise StravaError("Browser session fitness page returned HTML; structured stats need another adapter.")
        return self.get(f"athletes/{self.self_id()}/stats")

    def training(self, start_week, num_weeks=4):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_week) or not 1 <= num_weeks <= 52:
            raise StravaError("Use a YYYY-MM-DD start_week and 1–52 weeks.")
        if self._session()[1]:
            raise StravaError("Browser-session training-log endpoints returned 403.")
        return self.get(f"athletes/{self.self_id()}/training/log/entries",
                        {"start_week": start_week, "num_weeks": num_weeks})


store = CredentialStore()
mcp = FastMCP("Personal Strava", instructions=(
    "Read-only access to named Strava profiles. Private endpoints are experimental. "
    "Never request credentials through tools. Do not claim complete export coverage."))
READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}


def client_for(profile):
    return StravaClient(profile=profile, credential_store=store)


@mcp.tool(annotations=READ)
def strava_profiles() -> dict:
    """List configured profile names; never returns credential values."""
    return {"profiles": store.list_profiles()}


@mcp.tool(annotations=READ)
def strava_auth_status(profile: str = "default") -> dict:
    """Show whether a profile is configured and its credential type, without secrets."""
    return store.status(profile)


@mcp.tool(annotations=READ)
def strava_connect_instructions(profile: str = "default") -> dict:
    """Return safe local instructions for connecting a profile; never accept secrets here."""
    validate_profile(profile)
    return {"profile": profile, "credential_file": str(store.path_for(profile)),
            "schemas": [{"access_token": "YOUR_TOKEN"},
                        {"session_cookie": "YOUR_BROWSER_SESSION_COOKIE"}],
            "permissions": "Set the file mode to 600.",
            "browser_helper": "Run connect_session.py --profile " + profile,
            "warning": "Use your own credential; never paste it into chat or an MCP tool."}


@mcp.tool(annotations=READ)
def strava_profile(profile: str = "default") -> dict:
    """Read the authenticated athlete profile for a named credential profile."""
    return client_for(profile).profile()


@mcp.tool(annotations=READ)
def strava_activities(page: int = 1, query: str = "", profile: str = "default") -> dict:
    """Read activity history for a profile. Browser mode supports pagination; token mode supports keyword search."""
    return client_for(profile).activities(page, query)


@mcp.tool(annotations=READ)
def strava_activity(activity_id: int, profile: str = "default") -> dict:
    """Read an owned activity page and metrics after verifying ownership."""
    return client_for(profile).activity(activity_id)


@mcp.tool(annotations=READ)
def strava_streams(activity_id: int, profile: str = "default") -> list:
    """Read available GPS, time, heart rate, power, cadence and other streams."""
    return client_for(profile).streams(activity_id)


@mcp.tool(annotations=READ)
def strava_gear(profile: str = "default") -> list:
    """Read the authenticated athlete's gear."""
    return client_for(profile).gear()


@mcp.tool(annotations=READ)
def strava_stats(profile: str = "default") -> dict:
    """Read the authenticated athlete's statistics."""
    return client_for(profile).stats()


@mcp.tool(annotations=READ)
def strava_training(start_week: str, num_weeks: int = 4, profile: str = "default") -> dict:
    """Read training log entries available to a profile."""
    return client_for(profile).training(start_week, num_weeks)


@mcp.tool(annotations=READ)
def strava_query(kind: str, profile: str = "default", activity_id: int = 0,
                 page: int = 1, query: str = "", start_week: str = "",
                 num_weeks: int = 4) -> Any:
    """Dispatch a fixed, read-only query kind for clients that prefer one entry point."""
    client = client_for(profile)
    if kind == "profile":
        return client.profile()
    if kind == "activities":
        return client.activities(page, query)
    if kind == "activity" and activity_id > 0:
        return client.activity(activity_id)
    if kind == "streams" and activity_id > 0:
        return client.streams(activity_id)
    if kind == "gear":
        return client.gear()
    if kind == "stats":
        return client.stats()
    if kind == "training" and start_week:
        return client.training(start_week, num_weeks)
    raise StravaError("Unsupported query kind or missing required query fields.")


if __name__ == "__main__":
    mcp.run(transport="stdio")
