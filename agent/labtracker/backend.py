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


NOT_PUBLIC = (
    "Google wants a sign-in, so the web app isn't public. In Apps Script: Deploy -> Manage "
    "deployments -> edit (pencil) -> set 'Execute as: Me' and 'Who has access: Anyone' -> "
    "Version: New version -> Deploy. (If 'Anyone' isn't offered, your account is a work/school "
    "account that blocks public web apps; use a personal Gmail account instead.)")


def explain_google_reply(code, text: str) -> str:
    """Turn Google's HTML error pages into plain-English fixes."""
    t = (text or "").lower()
    if "accounts.google.com" in t or "servicelogin" in t or ">sign in<" in t:
        return NOT_PUBLIC
    if "script function not found" in t:
        return ("The script is missing doPost. Paste the whole of Code.gs, save, then Deploy -> "
                "Manage deployments -> edit -> Version: New version -> Deploy.")
    if "authorization is required" in t or "authorisation is required" in t:
        return ("The script hasn't been authorised yet. In Apps Script, choose doGet in the toolbar's "
                "function list, click Run and allow access, then try again.")
    if "need access" in t or "request access" in t or "permission" in t or code in (401, 403):
        return (f"Google refused the request ({code}). Check the deployment: 'Execute as: Me' and "
                "'Who has access: Anyone' (Deploy -> Manage deployments -> edit -> New version -> "
                "Deploy), and that you copied the URL ending in /exec, not /dev. If your Google "
                "account is a work or school account, its admin may block public web apps.")
    return f"Unexpected reply from Google ({code})."


def explain_backend_error(result: dict) -> str:
    err = result.get("error", "unknown")
    if err == "not_configured":
        return ("The Google script can't find a valid LT_AUTH_KEY. In Project Settings -> Script "
                "Properties, the property must be named exactly LT_AUTH_KEY and the value must be the "
                "whole key (44 characters, ending in =). Then click 'Save script properties'.")
    if err == "bad_signature":
        return ("LT_AUTH_KEY in Script Properties doesn't match the key on this laptop. Click 'Edit "
                "script properties', clear the value completely, paste the key exactly as shown by "
                "setup (first character to the final =), and save. Tip: in the editor, run "
                "labtrackerSelfTest to see the first and last characters Google has stored.")
    if err == "clock":
        skew = ""
        if isinstance(result.get("server_now"), (int, float)):
            mins = round((time.time() * 1000 - result["server_now"]) / 60000)
            skew = f" (it's {abs(mins)} min {'ahead' if mins > 0 else 'behind'})"
        return (f"Your laptop's clock is wrong{skew}. Open System Settings -> Date & Time and turn on "
                "network time, then try again.")
    if err == "replayed":
        return "A duplicate request was refused. Just try again."
    if err == "unauthorized":
        return ("The backend rejected the request. Check LT_AUTH_KEY in Script Properties and that "
                "your computer's clock is correct.")
    return f"The backend refused the request ({err})."


class _StrictRedirect(urllib.request.HTTPRedirectHandler):
    """Apps Script answers with a redirect to googleusercontent.com. Refuse any other host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if host == "accounts.google.com" or host.endswith(".accounts.google.com"):
            raise BackendError(NOT_PUBLIC)
        if urllib.parse.urlsplit(newurl).scheme != "https" or host not in ALLOWED_REDIRECT_HOSTS:
            raise BackendError(f"Refusing an unexpected redirect to {host or 'an unknown host'}.")
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
            try:
                text = e.read(20000).decode("utf-8", "replace")
            except Exception:
                text = ""
            raise BackendError(explain_google_reply(e.code, text)) from e
        except (urllib.error.URLError, OSError) as e:
            raise BackendError("Could not reach Google. Are you online?") from e
        try:
            result = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise BackendError(explain_google_reply(200, raw[:20000].decode("utf-8", "replace"))) from e
        if not result.get("ok"):
            raise BackendError(explain_backend_error(result))
        return result
