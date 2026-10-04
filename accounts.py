"""User accounts: sign up, sign in, and the profile behind them.

What an account is for. DASHBOARD_TOKEN stays the root of access: creating an
account needs that token, either from the link the person arrived on or typed
in as an access code. An account is the way back in afterwards, without
carrying the token around in a URL. Open sign-up would hand the project's logs
to anyone on the internet who found the page.

Where accounts live. Firestore on Cloud Run, because Cloud Run scales to zero
and forgets memory; in memory for local development. The account page says
which, so an account held in memory is never mistaken for a durable one. These
are OpsMind's own records -- nothing in the watched project changes.

Passwords. PBKDF2-HMAC-SHA256 from the standard library at 600,000 iterations
(OWASP's figure for this hash), a fresh salt per password, and the scheme
recorded in the stored hash so the cost can be raised later without breaking
old accounts. scrypt is avoided because the dev machine's LibreSSL lacks it.

Sessions. A signed cookie -- HMAC-SHA256 over a small JSON payload -- checked
without a database read, because the gate runs on every API call the dashboard
makes. The payload carries the display name so the nav can show it without a
lookup either.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from .config import settings

SESSION_COOKIE = "opsmind_session"
SESSION_MAX_AGE_S = 7 * 24 * 3600
MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 256
_MAX_NAME_LEN = 80
_PBKDF2_ITERATIONS = 600_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Failed sign-ins tolerated per address and per account before a pause.
_MAX_FAILURES = 10
_FAILURE_WINDOW_S = 15 * 60

# Only used when neither SESSION_SECRET nor DASHBOARD_TOKEN is set, i.e. a
# local run with the gate off. Sessions then end when the process does.
_PROCESS_SECRET = secrets.token_bytes(32)


class AccountError(Exception):
    """Something the person can fix. The message is shown to them as written."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AccountsUnavailable(Exception):
    """The account store could not be reached. Not the person's fault."""


# --- small helpers ----------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def _clean_name(name: Any) -> str:
    return " ".join(str(name or "").split())[:_MAX_NAME_LEN]


def _uid(email: str) -> str:
    # Derived from the email, so the document id itself enforces uniqueness:
    # two sign-ups racing for one address collide on create() instead of
    # producing two accounts.
    return "u_" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:24]


def initials(name: str, email: str = "") -> str:
    words = [w for w in (name or "").split() if w[:1].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    if words:
        return words[0][:2].upper()
    return (email[:1] or "?").upper()


# --- passwords --------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (_PBKDF2_ITERATIONS, _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, want = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 _unb64(salt), int(iterations))
        return hmac.compare_digest(dk, _unb64(want))
    except Exception:  # noqa: BLE001 - a malformed hash is a failed match
        return False


# Spent on sign-ins for an unknown email, so a wrong address and a wrong
# password take the same time and the response cannot reveal who has an account.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _check_password(password: Any) -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD_LEN:
        raise AccountError("Use at least %d characters for the password."
                           % MIN_PASSWORD_LEN)
    if len(password) > _MAX_PASSWORD_LEN:
        raise AccountError("That password is too long.")
    return password


# --- stores -----------------------------------------------------------------

class _MemoryStore:
    backend = "memory"
    persistent = False

    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._rows.get(uid)
            return dict(row) if row else None

    def create(self, uid: str, row: Dict[str, Any]) -> None:
        with self._lock:
            if uid in self._rows:
                raise AccountError("An account with this email already exists. "
                                   "Sign in instead.", 409)
            self._rows[uid] = dict(row)

    def update(self, uid: str, fields: Dict[str, Any]) -> None:
        with self._lock:
            if uid not in self._rows:
                raise AccountError("This account no longer exists.", 404)
            self._rows[uid].update(fields)


class _FirestoreStore:
    backend = "firestore"
    persistent = True

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    def _docs(self) -> Any:
        # The client is built on first use, not at import, and a failure is
        # not remembered: if the database is created after the portal boots,
        # the next request simply works.
        with self._lock:
            if self._client is None:
                try:
                    from google.cloud import firestore
                    self._client = firestore.Client(
                        project=settings.project_id or None,
                        database=settings.firestore_database)
                except Exception as exc:  # noqa: BLE001
                    raise AccountsUnavailable(_describe(exc))
        return self._client.collection(settings.accounts_collection)

    def get(self, uid: str) -> Optional[Dict[str, Any]]:
        docs = self._docs()
        try:
            snap = docs.document(uid).get()
        except Exception as exc:  # noqa: BLE001
            raise AccountsUnavailable(_describe(exc))
        return snap.to_dict() if snap.exists else None

    def create(self, uid: str, row: Dict[str, Any]) -> None:
        docs = self._docs()
        from google.api_core.exceptions import AlreadyExists
        try:
            docs.document(uid).create(row)
        except AlreadyExists:
            raise AccountError("An account with this email already exists. "
                               "Sign in instead.", 409)
        except Exception as exc:  # noqa: BLE001
            raise AccountsUnavailable(_describe(exc))

    def update(self, uid: str, fields: Dict[str, Any]) -> None:
        docs = self._docs()
        from google.api_core.exceptions import NotFound
        try:
            docs.document(uid).update(fields)
        except NotFound:
            raise AccountError("This account no longer exists.", 404)
        except Exception as exc:  # noqa: BLE001
            raise AccountsUnavailable(_describe(exc))


def _describe(exc: Exception) -> str:
    return "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:200]
                       if str(exc) else "")


_store: Any = None
_store_lock = threading.Lock()


def _backend() -> Any:
    global _store
    with _store_lock:
        if _store is None:
            _store = (_FirestoreStore() if settings.accounts_backend == "firestore"
                      else _MemoryStore())
        return _store


def status() -> Dict[str, Any]:
    store = _backend()
    return {"backend": store.backend, "persistent": store.persistent}


# --- throttling -------------------------------------------------------------

_failures: Dict[str, List[float]] = {}
_failures_lock = threading.Lock()


def _recent(key: str, now: float) -> List[float]:
    stamps = [t for t in _failures.get(key, []) if now - t < _FAILURE_WINDOW_S]
    if stamps:
        _failures[key] = stamps
    else:
        _failures.pop(key, None)
    return stamps


def _throttled(keys: List[str]) -> bool:
    now = time.time()
    with _failures_lock:
        return any(len(_recent(k, now)) >= _MAX_FAILURES for k in keys)


def _record_failure(keys: List[str]) -> None:
    now = time.time()
    with _failures_lock:
        if len(_failures) > 10000:  # bound the memory an attacker can make us hold
            _failures.clear()
        for k in keys:
            _failures.setdefault(k, []).append(now)


# --- operations -------------------------------------------------------------

def public(row: Dict[str, Any]) -> Dict[str, Any]:
    """What the browser may see. Never the password hash."""
    return {
        "id": row.get("uid"),
        "name": row.get("name") or "",
        "email": row.get("email") or "",
        "initials": initials(row.get("name") or "", row.get("email") or ""),
        "createdAt": row.get("createdAt"),
        "lastSignInAt": row.get("lastSignInAt"),
        "passwordChangedAt": row.get("passwordChangedAt"),
    }


def sign_up(name: Any, email: Any, password: Any) -> Dict[str, Any]:
    name = _clean_name(name)
    email = normalize_email(email)
    if not name:
        raise AccountError("Enter your name.")
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise AccountError("Enter a valid email address.")
    password = _check_password(password)

    now = time.time()
    row = {
        "uid": _uid(email), "email": email, "name": name,
        "passwordHash": hash_password(password),
        "createdAt": now, "lastSignInAt": now, "passwordChangedAt": now,
    }
    _backend().create(row["uid"], row)
    return row


def sign_in(email: Any, password: Any, client_ip: str = "") -> Dict[str, Any]:
    email = normalize_email(email)
    password = str(password or "")
    keys = ["ip:" + (client_ip or "?"), "email:" + email]
    if _throttled(keys):
        raise AccountError("Too many attempts. Wait a few minutes and try again.", 429)

    row = _backend().get(_uid(email)) if email else None
    if row is None:
        verify_password(password, _DUMMY_HASH)
        ok = False
    else:
        ok = verify_password(password[:_MAX_PASSWORD_LEN], row.get("passwordHash", ""))
    if not ok:
        _record_failure(keys)
        raise AccountError("That email and password do not match an account.", 401)

    now = time.time()
    _backend().update(row["uid"], {"lastSignInAt": now})
    row["lastSignInAt"] = now
    return row


def get(uid: str) -> Optional[Dict[str, Any]]:
    return _backend().get(uid) if uid else None


def rename(uid: str, name: Any) -> Dict[str, Any]:
    name = _clean_name(name)
    if not name:
        raise AccountError("Enter your name.")
    _backend().update(uid, {"name": name})
    row = _backend().get(uid)
    if row is None:
        raise AccountError("This account no longer exists.", 404)
    return row


def change_password(uid: str, current: Any, new: Any) -> Dict[str, Any]:
    row = _backend().get(uid)
    if row is None:
        raise AccountError("This account no longer exists.", 404)
    if not verify_password(str(current or "")[:_MAX_PASSWORD_LEN],
                           row.get("passwordHash", "")):
        raise AccountError("Your current password is not right.", 403)
    new = _check_password(new)
    now = time.time()
    fields = {"passwordHash": hash_password(new), "passwordChangedAt": now}
    _backend().update(uid, fields)
    row.update(fields)
    return row


# --- sessions ---------------------------------------------------------------

def _secret() -> bytes:
    if settings.session_secret:
        return settings.session_secret.encode("utf-8")
    if settings.dashboard_token:
        # Derived, not the token itself, so a leaked cookie signature says
        # nothing about the token. Rotating the token signs everyone out.
        return hmac.new(settings.dashboard_token.encode("utf-8"),
                        b"opsmind-session-v1", hashlib.sha256).digest()
    return _PROCESS_SECRET


def _sign(body: str) -> str:
    return _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())


def issue_session(row: Dict[str, Any]) -> str:
    payload = {"u": row["uid"], "n": row.get("name") or "",
               "e": row.get("email") or "",
               "x": int(time.time()) + SESSION_MAX_AGE_S}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return body + "." + _sign(body)


def read_session(token: str) -> Optional[Dict[str, Any]]:
    """The signed-in person, from the cookie alone, or None."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        if not hmac.compare_digest(sig.encode("ascii"), _sign(body).encode("ascii")):
            return None
        payload = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001 - a mangled cookie is no session
        return None
    if not isinstance(payload, dict) or payload.get("x", 0) < time.time():
        return None
    name, email = payload.get("n") or "", payload.get("e") or ""
    return {"id": payload.get("u"), "name": name, "email": email,
            "initials": initials(name, email)}
