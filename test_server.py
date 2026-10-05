import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from server import AuthCredentials, CredentialStore, StravaClient, StravaError


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Path(self.tmp.name) / "session.json"
        self.session.write_text(json.dumps({"access_token": "test-token"}))
        self.session.chmod(0o600)
        self.requests = []

    def client(self, status=200, owner=7):
        def respond(request):
            self.requests.append(request)
            if request.url.path.endswith("/athlete"):
                data = {"id": 7}
            else:
                data = {"id": 123, "athlete": {"id": owner}}
            return httpx.Response(status, json=data)
        return StravaClient(self.session, httpx.MockTransport(respond), interval=0)

    def test_self_activity_and_auth(self):
        self.assertEqual(self.client().activity(123)["id"], 123)
        self.assertTrue(all(r.method == "GET" for r in self.requests))
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer test-token")
        self.assertEqual(self.requests[0].url.host, "cdn-1.strava.com")

    def test_foreign_activity_streams_denied(self):
        with self.assertRaises(StravaError):
            self.client(owner=8).streams(123)
        self.assertFalse(any("streams" in r.url.path for r in self.requests))

    def test_redirect_not_followed(self):
        with self.assertRaises(StravaError):
            self.client(status=302).profile()
        self.assertEqual(len(self.requests), 1)

    def test_rate_limit_not_retried(self):
        with self.assertRaisesRegex(StravaError, "rate limit"):
            self.client(status=429).profile()
        self.assertEqual(len(self.requests), 1)

    def test_cookie_host(self):
        self.session.write_text(json.dumps({"session_cookie": "test-cookie"}))
        self.client()._web_get("/athlete/mentionable_athletes")
        self.assertEqual(self.requests[0].url.host, "www.strava.com")
        self.assertEqual(self.requests[0].headers["Cookie"], "_strava4_session=test-cookie")

    def test_permissions(self):
        self.session.chmod(0o644)
        with self.assertRaisesRegex(StravaError, "600"):
            self.client().profile()

    def test_no_url_injection(self):
        with self.assertRaises(StravaError):
            self.client().get("https://example.org")
        self.assertEqual(self.requests, [])

    def test_errors_do_not_expose_token(self):
        with self.assertRaises(StravaError) as result:
            self.client(status=401).profile()
        self.assertNotIn("test-token", str(result.exception))

    def test_named_profiles_use_isolated_credentials(self):
        profiles = Path(self.tmp.name) / "profiles"
        profiles.mkdir()
        first = profiles / "alice.json"
        second = profiles / "bob.json"
        first.write_text(json.dumps({"access_token": "alice-token"}))
        second.write_text(json.dumps({"session_cookie": "bob-cookie"}))
        first.chmod(0o600)
        second.chmod(0o600)
        store = CredentialStore(credentials_dir=profiles)

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={"id": 7})

        StravaClient(profile="alice", credential_store=store,
                     transport=httpx.MockTransport(respond), interval=0).get("athlete")
        StravaClient(profile="bob", credential_store=store,
                     transport=httpx.MockTransport(respond), interval=0)._web_get(
                         "/athlete/mentionable_athletes")
        self.assertEqual(self.requests[0].url.host, "cdn-1.strava.com")
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer alice-token")
        self.assertEqual(self.requests[1].url.host, "www.strava.com")
        self.assertEqual(self.requests[1].headers["Cookie"], "_strava4_session=bob-cookie")

    def test_profile_name_traversal_is_rejected(self):
        with self.assertRaisesRegex(StravaError, "profile name"):
            CredentialStore(credentials_dir=self.tmp.name).status("../secret")

    def test_profile_status_has_no_secret(self):
        profiles = Path(self.tmp.name) / "profiles"
        profiles.mkdir()
        path = profiles / "alice.json"
        path.write_text(json.dumps({"access_token": "secret-token"}))
        path.chmod(0o600)
        result = CredentialStore(credentials_dir=profiles).status("alice")
        self.assertEqual(result["credential_kind"], "access_token")
        self.assertNotIn("secret-token", json.dumps(result))


class EnvironmentCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"

    def write_env(self, text, mode=0o600):
        self.env_file.write_text(text)
        self.env_file.chmod(mode)

    def test_email_password_loaded_from_env_file(self):
        self.write_env("STRAVA_EMAIL=runner@example.com\nSTRAVA_PASSWORD='secret pass'\n")
        store = CredentialStore(env_file=self.env_file)
        credentials = store.credentials()
        self.assertEqual(credentials, AuthCredentials(
            "email_password", "runner@example.com", "secret pass"))
        self.assertEqual(store.status(), {
            "profile": "default", "configured": True,
            "credential_kind": "email_password", "source": "environment", "path": None})
        self.assertEqual(store.list_profiles(), ["default"])
        self.assertNotIn("secret", json.dumps(store.status()))

    def test_process_environment_overrides_env_file(self):
        self.write_env("STRAVA_EMAIL=file@example.com\nSTRAVA_PASSWORD=file-pass\n")
        with patch.dict(os.environ, {"STRAVA_EMAIL": "process@example.com",
                                     "STRAVA_PASSWORD": "process-pass"}, clear=False):
            credentials = CredentialStore(env_file=self.env_file).credentials()
        self.assertEqual(credentials.email, "process@example.com")
        self.assertEqual(credentials.password, "process-pass")

    def test_partial_login_configuration_is_rejected_without_secret(self):
        self.write_env("STRAVA_EMAIL=runner@example.com\n")
        store = CredentialStore(env_file=self.env_file)
        with self.assertRaisesRegex(StravaError, "both STRAVA_EMAIL and STRAVA_PASSWORD"):
            store.credentials()
        status = store.status()
        self.assertFalse(status["configured"])
        self.assertNotIn("runner@example.com", json.dumps(status))

    def test_env_file_permissions_are_required(self):
        self.write_env("STRAVA_EMAIL=runner@example.com\nSTRAVA_PASSWORD=secret\n", 0o644)
        with self.assertRaisesRegex(StravaError, "permissions 600"):
            CredentialStore(env_file=self.env_file)

    def test_login_and_token_configuration_cannot_be_mixed(self):
        self.write_env("STRAVA_EMAIL=runner@example.com\nSTRAVA_PASSWORD=secret\n"
                       "STRAVA_ACCESS_TOKEN=token\n")
        with self.assertRaisesRegex(StravaError, "either token/cookie or email/password"):
            CredentialStore(env_file=self.env_file).credentials()


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"
        self.env_file.write_text("STRAVA_EMAIL=runner@example.com\nSTRAVA_PASSWORD=secret-pass\n")
        self.env_file.chmod(0o600)
        self.requests = []

    def client(self, handler):
        store = CredentialStore(env_file=self.env_file)
        return StravaClient(credential_store=store,
                            transport=httpx.MockTransport(handler), interval=0)

    def test_login_form_post_and_authenticated_api_request(self):
        def respond(request):
            self.requests.append(request)
            if request.url.path == "/login":
                return httpx.Response(200, text=(
                    '<form action="/session" method="post">'
                    '<input type="hidden" name="utf8" value="✓">'
                    '<input type="hidden" name="authenticity_token" value="csrf-token">'
                    '<input name="email"><input name="password" type="password">'
                    '</form>'))
            if request.url.path == "/session":
                form = httpx.QueryParams(request.content.decode())
                self.assertEqual(form["email"], "runner@example.com")
                self.assertEqual(form["password"], "secret-pass")
                self.assertEqual(form["authenticity_token"], "csrf-token")
                return httpx.Response(302, headers={
                    "Location": "/dashboard",
                    "Set-Cookie": "_strava4_session=logged-in-cookie; Path=/"})
            if request.url.path == "/api/v3/athlete":
                self.assertIn("_strava4_session=logged-in-cookie",
                              request.headers.get("Cookie", ""))
                self.assertNotIn("Authorization", request.headers)
                return httpx.Response(200, json={"id": 7})
            raise AssertionError(f"unexpected request: {request.url}")

        client = self.client(respond)
        self.assertEqual(client.get("athlete"), {"id": 7})
        self.assertEqual(client.get("athlete"), {"id": 7})
        self.assertEqual([request.url.path for request in self.requests],
                         ["/login", "/session", "/api/v3/athlete", "/api/v3/athlete"])

    def test_failed_login_does_not_expose_password(self):
        def respond(request):
            if request.url.path == "/login":
                return httpx.Response(200, text='<form action="/session"><input name="authenticity_token" value="csrf"></form>')
            return httpx.Response(200, text="invalid credentials")

        with self.assertRaises(StravaError) as result:
            self.client(respond).get("athlete")
        self.assertNotIn("secret-pass", str(result.exception))

    def test_login_form_endpoint_is_validated(self):
        def respond(request):
            return httpx.Response(200, text='<form action="https://example.org/session"></form>')

        with self.assertRaisesRegex(StravaError, "unsupported endpoint"):
            self.client(respond).get("athlete")

    def test_email_login_uses_web_activity_history(self):
        def respond(request):
            self.requests.append(request)
            if request.url.path == "/login":
                return httpx.Response(200, text='<form action="/session"></form>')
            if request.url.path == "/session":
                return httpx.Response(302, headers={
                    "Location": "/dashboard",
                    "Set-Cookie": "_strava4_session=logged-in-cookie; Path=/"})
            if request.url.path == "/athlete/training_activities":
                self.assertIn("_strava4_session=logged-in-cookie",
                              request.headers.get("Cookie", ""))
                return httpx.Response(200, json={"models": []})
            raise AssertionError(f"unexpected request: {request.url}")

        self.assertEqual(self.client(respond).activities(), {"models": []})
        self.assertEqual([request.url.path for request in self.requests],
                         ["/login", "/session", "/athlete/training_activities"])


if __name__ == "__main__":
    unittest.main()
