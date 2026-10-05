# Strava MCP — read-only, multi-profile adapter

This server exposes named Strava credential profiles through read-only MCP
tools. Credentials stay local: they are never accepted as MCP tool arguments
and are never included in responses.

## Run

The tested environment is:

    /tmp/strava-research/venv/bin/python /home/armistice/.ssh/strava-mcp/server.py

The simplest setup is a mode-600 `.env` next to `server.py`:

    STRAVA_EMAIL=you@example.com
    STRAVA_PASSWORD=YOUR_PASSWORD

The server logs in to Strava's web session once per process and keeps the
resulting session cookie in memory. Set STRAVA_ENV_FILE to use a different
env-file path. Process environment variables override values from the file.

For compatibility, profiles are also loaded from:

    /home/armistice/.ssh/strava-mcp/profiles/<profile>.json

Set STRAVA_CREDENTIALS_DIR to use another directory. Each profile file must be
mode 600 and contain exactly one of these objects:

    {"access_token": "YOUR_TOKEN"}
    {"session_cookie": "YOUR_BROWSER_SESSION_COOKIE"}

For compatibility, the original STRAVA_SESSION_FILE environment variable and
the project-local session.json are still accepted for the default profile.
STRAVA_ACCESS_TOKEN or STRAVA_SESSION_COOKIE can also configure only the
default profile. Do not put a credential in chat, source control, or an MCP
tool call. Email/password mode is only available for accounts that can use a
Strava password; Google, Apple, and one-time-code-only accounts need another
supported credential.

## Connect a browser profile

The helper imports only the _strava4_session cookie from a local Brave or
Chromium profile and writes a mode-600 profile file. It does not print the
cookie:

    /tmp/strava-research/venv/bin/python connect_session.py --profile alice --browser brave

Use --credentials-dir when profiles should live outside the project. Token
users can create the JSON file directly with the schema above, preferably via
an OS secret-management workflow.

## MCP tools

The server provides strava_profiles, strava_auth_status, and
strava_connect_instructions for profile discovery and setup. Data tools accept
profile="name": strava_profile, strava_activities, strava_activity,
strava_streams, strava_gear, strava_stats, strava_training, and the fixed
read-only dispatcher strava_query.

The dispatcher accepts only these kinds: profile, activities, activity,
streams, gear, stats, and training. There is no arbitrary endpoint tool.

For a shared MCP process, the process owner must protect the credentials
directory because a profile selector can access any profile configured for that
process. The safer multi-person deployment is one server process and profile
directory per user.

## Current coverage and limits

The browser-session adapter was live-tested against the authorized account:
activity history pagination and activity pages returned data, while streams,
laps, and training-log routes were denied or unavailable. API-token mode is
implemented for athlete, activity search/details, streams, gear, statistics,
and training-log reads, subject to Strava account permissions. Email/password
mode uses the same read-only web-session adapter after logging in. This remains
a private, reverse-engineered adapter rather than an official Strava API
client; coverage is not guaranteed to be complete or stable.

The adapter uses only fixed GET routes, does not follow redirects, does not
retry authentication/rate-limit failures, verifies activity ownership before
returning activity details or streams, and enforces request spacing.

## Development

Run tests from this directory:

    /tmp/strava-research/venv/bin/python -m unittest -v test_server.py

The implementation was informed by static analysis of the installed Strava
483.10 Android APK with Ghidra and JADX. The original research artifacts and
read-only surface audit remain alongside this project.
