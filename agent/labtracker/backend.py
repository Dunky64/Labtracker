"""Signed HTTPS client for the Google Apps Script backend."""
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl

from . import crypto

ALLOWED_PREFIX = "https://script.google.com/macros/s/"
ALLOWED_REDIRECT_HOSTS = {"script.google.com", "script.googleusercontent.com"}


class BackendError(RuntimeError):
    pass


def valid_webapp_url(url: str) -> bool:
    if not url.startswith(ALLOWED_PREFIX) or not url.endswith("/exec"):
        return False
    deployment = url[len(ALLOWED_PREFIX):-len("/exec")]
    return 20 <= len(deployment) <= 200 and all(c.isalnum() or c in "-_" for c in deployment)


class _StrictRedirect(urllib.request.HTTPRedirectHandler):
    """Apps Script answers with a redirect to googleusercontent.com. Refuse any other host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if urllib.parse.urlsplit(newurl).scheme != "https" or host not in ALLOWED_REDIRECT_HOSTS:
            raise BackendError("Refusing an unexpected redirect.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Backend:
    def __init__(self, url: str, auth_key: bytes, timeout: float = 25, _test_mode: bool = False):
        if not _test_mode and not valid_webapp_url(url):
            raise BackendError("That doesn't look like an Apps Script web app URL.")
        self.url = url
        self.auth_key = auth_key
        self.timeout = timeout
        ctx = ssl.create_default_context()  # verifies certificates and hostnames
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = urllib.request.build_opener(
            _StrictRedirect(), urllib.request.HTTPSHandler(context=ctx))

    def call(self, action: str, **data) -> dict:
        payload = json.dumps(dict(data, action=action), separators=(",", ":"))
        ts = int(time.time() * 1000)
        nonce = crypto.b64e(secrets.token_bytes(18))
        body = json.dumps({
            "v": 1, "ts": ts, "nonce": nonce, "payload": payload,
            "sig": crypto.sign(self.auth_key, ts, nonce, payload),
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "text/plain;charset=utf-8", "User-Agent": "labtracker-agent"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(5_000_000)
        except urllib.error.HTTPError as e:
            raise BackendError(f"Server error ({e.code}).") from e
        except (urllib.error.URLError, OSError) as e:
            raise BackendError("Could not reach Google. Are you online?") from e
        try:
            result = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise BackendError(
                "Unexpected reply. Check the web app is deployed with access set to 'Anyone'.") from e
        if not result.get("ok"):
            err = result.get("error", "unknown")
            if err == "unauthorized":
                raise BackendError(
                    "The backend rejected the request. Check LT_AUTH_KEY in Script Properties "
                    "and that your computer's clock is correct.")
            raise BackendError(f"The backend refused the request ({err}).")
        return result
