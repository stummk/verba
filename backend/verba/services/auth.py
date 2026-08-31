"""Optional user management: accounts, login sessions and project access.

The whole feature is a switch. While `settings.auth.enabled` is False Verba
behaves exactly as it always did — no login, everybody may do everything —
which is the only sensible mode for the local desktop build. Switching it on
creates the first administrator and hands every existing transcript to them;
nothing is deleted or rewritten in the process, so an installation can be
upgraded and secured in two independent steps.

Passwords are hashed with scrypt from the standard library (no extra
dependency, `requirements/core.txt` stays small). Sessions live in the
database and travel as an HttpOnly cookie; only their SHA-256 is stored, so a
copied database file hands out no live sessions.

Access model (three visibilities, full access for whoever can see a project):

    private  owner and administrators
    shared   owner, administrators and the explicitly named users
    public   every logged-in user

Whoever may see a transcript may also edit and delete it. The two exceptions
are ownership and visibility themselves — only the owner and administrators
may change those, otherwise any user could quietly turn a public transcript
private and lock everyone else out of it.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import config, db

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

VISIBILITIES = ("private", "shared", "public")

COOKIE_NAME = "verba_session"
MIN_PASSWORD_LENGTH = 8

# scrypt parameters: ~16 MiB and roughly 50 ms per hash on a current CPU —
# enough to make an offline attack on the stolen database expensive without
# turning the login into a noticeable wait.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# Never selected: password_hash and password_salt leave the service layer
# nowhere, not even into a log line.
USER_COLUMNS = "id, username, display_name, role, must_change_password, created_at, last_login_at"


class AuthError(RuntimeError):
    """A rule of the user management was violated; the message is shown in the UI."""


# ── passwords ─────────────────────────────────────────────────────────


def hash_password(password: str, salt: str = "") -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    try:
        candidate, _ = hash_password(password, salt)
    except ValueError:  # a salt that is not hex — a hand-edited database row
        return False
    return secrets.compare_digest(candidate, password_hash)


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.")
    return password


def validate_username(username: str) -> str:
    cleaned = username.strip()
    if not cleaned:
        raise AuthError("Der Benutzername darf nicht leer sein.")
    if len(cleaned) > 64:
        raise AuthError("Der Benutzername darf höchstens 64 Zeichen lang sein.")
    return cleaned


# ── state ─────────────────────────────────────────────────────────────


def enabled() -> bool:
    return config.get_settings().auth.enabled


def user_count() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def admin_count(exclude_id: int | None = None) -> int:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = ? AND id IS NOT ?",
            (ROLE_ADMIN, exclude_id),
        ).fetchone()["n"]


def oldest_admin(exclude_id: int | None = None) -> dict[str, Any] | None:
    """The longest-serving administrator — the fallback owner for orphaned work."""
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE role = ? AND id IS NOT ? "
            "ORDER BY id ASC LIMIT 1",
            (ROLE_ADMIN, exclude_id),
        ).fetchone()
    return db.row_to_dict(row)


# ── users ─────────────────────────────────────────────────────────────


def list_users() -> list[dict[str, Any]]:
    """All accounts with the number of transcripts each one owns."""
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT {USER_COLUMNS}, "
            "(SELECT COUNT(*) FROM projects p WHERE p.owner_id = users.id) AS project_count "
            "FROM users ORDER BY id"
        ).fetchall()
    return db.rows_to_dicts(rows)


def get_user(user_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
    return db.row_to_dict(row)


def get_user_by_name(username: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
    return db.row_to_dict(row)


def create_user(
    username: str,
    password: str,
    role: str = ROLE_USER,
    display_name: str = "",
    must_change_password: bool = True,
) -> dict[str, Any]:
    """Create an account. Only administrators call this — there is no
    self-registration, so nobody who merely reaches the port gets an account."""
    username = validate_username(username)
    validate_password(password)
    if role not in ROLES:
        raise AuthError(f"Unbekannte Rolle: {role}")
    password_hash, salt = hash_password(password)
    with db.get_conn() as conn:
        taken = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if taken is not None:
            raise AuthError(f"Der Benutzername „{username}“ ist bereits vergeben.")
        cursor = conn.execute(
            "INSERT INTO users (username, display_name, password_hash, password_salt, role, "
            "must_change_password) VALUES (?, ?, ?, ?, ?, ?)",
            (
                username,
                display_name.strip(),
                password_hash,
                salt,
                role,
                int(must_change_password),
            ),
        )
        user_id = cursor.lastrowid
    logger.info("user created: %s (role %s)", username, role)
    return get_user(user_id)  # type: ignore[return-value]


def update_user(
    user_id: int,
    *,
    display_name: str | None = None,
    role: str | None = None,
    password: str | None = None,
) -> dict[str, Any] | None:
    """Change an account. Demoting the last administrator is refused — nobody
    would be left who could manage users or settings."""
    user = get_user(user_id)
    if user is None:
        return None
    fields: dict[str, Any] = {}
    if display_name is not None:
        fields["display_name"] = display_name.strip()
    if role is not None:
        if role not in ROLES:
            raise AuthError(f"Unbekannte Rolle: {role}")
        if user["role"] == ROLE_ADMIN and role != ROLE_ADMIN and admin_count(user_id) == 0:
            raise AuthError(
                "Das ist das letzte Administratorkonto — ohne Administrator könnte niemand "
                "mehr Nutzer oder Einstellungen verwalten."
            )
        fields["role"] = role
    if password is not None:
        validate_password(password)
        fields["password_hash"], fields["password_salt"] = hash_password(password)
        # a password the user chose themselves clears the change reminder; an
        # administrator resetting it sets the flag again via set_must_change
        fields["must_change_password"] = 0
    if not fields:
        return user

    assignments = ", ".join(f"{key} = ?" for key in fields)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", [*fields.values(), user_id])
    if password is not None:
        # every other device is logged out: a changed password must end the
        # sessions it was changed because of
        destroy_user_sessions(user_id)
    return get_user(user_id)


def set_must_change_password(user_id: int, value: bool = True) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET must_change_password = ? WHERE id = ?", (int(value), user_id)
        )


def delete_user(user_id: int) -> dict[str, int]:
    """Remove an account and settle its transcripts.

    Private transcripts belonged to this person alone and go with them, files
    and all. Shared and public ones are other people's working material, so
    they change hands to the longest-serving remaining administrator instead
    of disappearing from under them. Returns the counts for the confirmation
    message.
    """
    user = get_user(user_id)
    if user is None:
        raise AuthError("Der Nutzer existiert nicht.")
    if user["role"] == ROLE_ADMIN and admin_count(user_id) == 0:
        raise AuthError(
            "Das ist das letzte Administratorkonto — es kann nicht gelöscht werden, weil danach "
            "niemand mehr Nutzer oder Einstellungen verwalten könnte."
        )
    successor = oldest_admin(exclude_id=user_id)

    with db.get_conn() as conn:
        private_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM projects WHERE owner_id = ? AND visibility = 'private'",
                (user_id,),
            )
        ]
        transferred = conn.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE owner_id = ? AND visibility != 'private'",
            (user_id,),
        ).fetchone()["n"]

    from . import workspace  # local import: workspace imports this module too

    for project_id in private_ids:
        workspace.delete_project(project_id, delete_files=True)

    with db.get_conn() as conn:
        # everything still owned by them is public or shared: hand it over
        conn.execute(
            "UPDATE projects SET owner_id = ? WHERE owner_id = ?",
            (successor["id"] if successor else None, user_id),
        )
        # sessions and the shares granted *to* this user cascade away
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    logger.info(
        "user deleted: %s (%d transcripts removed, %d transferred)",
        user["username"],
        len(private_ids),
        transferred,
    )
    return {"deleted_projects": len(private_ids), "transferred_projects": transferred}


# ── sessions ──────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, password_salt FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    if row is None:
        # spend the same time as a real check so the response time does not
        # tell an attacker which usernames exist
        hash_password(password)
        return None
    if not verify_password(password, row["password_hash"], row["password_salt"]):
        return None
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (row["id"],))
    return get_user(row["id"])


def check_password(user_id: int, password: str) -> bool:
    """Re-authenticate an already logged-in user (password change, self-delete).

    Separate from `authenticate` because this is not a login: it must not move
    the account's last-login timestamp.
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash, password_salt FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return False
    return verify_password(password, row["password_hash"], row["password_salt"])


def create_session(user_id: int) -> str:
    """Start a session and return the plaintext token for the cookie."""
    token = secrets.token_urlsafe(32)
    days = config.get_settings().auth.session_days
    expires = (_now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, expires),
        )
    return token


def session_user(token: str) -> dict[str, Any] | None:
    """The user behind a cookie value, or None for missing/expired/unknown."""
    if not token:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > datetime('now')",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE sessions SET last_seen_at = datetime('now') WHERE token_hash = ?",
            (_token_hash(token),),
        )
    return get_user(row["user_id"])


def destroy_session(token: str) -> None:
    if not token:
        return
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def destroy_user_sessions(user_id: int) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired_sessions() -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")


# ── switching the feature on and off ──────────────────────────────────


def enable(username: str, password: str, display_name: str = "") -> dict[str, Any]:
    """Create the first administrator and switch the user management on.

    Everything that already exists keeps existing: the transcripts stay
    exactly where they are, keep their (public) visibility and are handed to
    this first administrator, who can then sort out who owns what.
    """
    if enabled():
        raise AuthError("Die Nutzerverwaltung ist bereits aktiv.")
    if user_count():
        raise AuthError("Es existieren bereits Nutzerkonten.")
    admin = create_user(
        username,
        password,
        role=ROLE_ADMIN,
        display_name=display_name,
        must_change_password=False,
    )
    with db.get_conn() as conn:
        adopted = conn.execute(
            "UPDATE projects SET owner_id = ? WHERE owner_id IS NULL", (admin["id"],)
        ).rowcount
    settings = config.get_settings()
    settings.auth.enabled = True
    config.save_settings(settings)
    logger.info("user management enabled; %d existing transcripts adopted by %s", adopted, username)
    return {"user": admin, "adopted_projects": adopted}


def disable() -> None:
    """Switch the user management off again (administrators only).

    The accounts survive so it can be switched back on without setting
    everybody up a second time; the live sessions do not, because from now on
    there is nothing left for them to authorise.
    """
    settings = config.get_settings()
    settings.auth.enabled = False
    config.save_settings(settings)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions")
    logger.warning("user management disabled — every request is unauthenticated again")


# ── project access ────────────────────────────────────────────────────


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") == ROLE_ADMIN


def visibility_clause(user: dict[str, Any] | None, alias: str = "p") -> tuple[str, list[Any]]:
    """SQL condition restricting a `projects` row to what `user` may reach.

    Returns ("", []) when everything is allowed, so callers can append it
    unconditionally. Fails closed: no user while the management is on means
    no rows, which is what an unauthenticated request must see even if some
    future caller forgets its own check.
    """
    if not enabled():
        return "", []
    if user is None:
        return "0", []
    if is_admin(user):
        return "", []
    return (
        f"({alias}.visibility = 'public' OR {alias}.owner_id = ? OR "
        f"({alias}.visibility = 'shared' AND EXISTS (SELECT 1 FROM project_shares s "
        f"WHERE s.project_id = {alias}.id AND s.user_id = ?)))",
        [user["id"], user["id"]],
    )


def can_access(project: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
    """May this user use the transcript at all? Seeing it means full access."""
    if project is None:
        return False
    if not enabled():
        return True
    if user is None:
        return False
    if is_admin(user) or project.get("owner_id") == user["id"]:
        return True
    visibility = project.get("visibility") or "public"
    if visibility == "public":
        return True
    if visibility == "shared":
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM project_shares WHERE project_id = ? AND user_id = ?",
                (project["id"], user["id"]),
            ).fetchone()
        return row is not None
    return False


def can_administer(project: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
    """May this user change the transcript's owner, visibility or share list?

    Deliberately narrower than `can_access`: with full access for everyone who
    can see a transcript, a co-worker could otherwise set a public one to
    private and lock the rest of the team out of their own material.
    """
    if project is None:
        return False
    if not enabled():
        return True
    if user is None:
        return False
    return is_admin(user) or project.get("owner_id") == user["id"]


def list_shares(project_id: int) -> list[int]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM project_shares WHERE project_id = ? ORDER BY user_id",
            (project_id,),
        ).fetchall()
    return [row["user_id"] for row in rows]


def set_shares(project_id: int, user_ids: list[int]) -> list[int]:
    """Replace the share list of a project; unknown user ids are dropped."""
    with db.get_conn() as conn:
        known = {
            row["id"] for row in conn.execute("SELECT id FROM users") if row["id"] in set(user_ids)
        }
        conn.execute("DELETE FROM project_shares WHERE project_id = ?", (project_id,))
        conn.executemany(
            "INSERT INTO project_shares (project_id, user_id) VALUES (?, ?)",
            [(project_id, user_id) for user_id in sorted(known)],
        )
    return sorted(known)
