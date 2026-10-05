import json
import tempfile
import unittest
from pathlib import Path
import httpx
from server import CredentialStore, StravaClient, StravaError


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


if __name__ == "__main__":
    unittest.main()
