"""Offline password-reset CLI for a locally-installed wattracker.

Usage:
    python -m wattracker.reset_password <username>   # reset one user's password
    python -m wattracker.reset_password --list       # list usernames only

Passwords are scrypt-hashed with no email/recovery path, so a forgotten
password would otherwise strand the user's data. This tool re-hashes a new
password directly into ``users.password_hash`` using the exact same
``wattracker.auth.hash_password`` primitive (same scrypt params/format) that
``/register`` uses -- there is no second hashing path.

Security model
--------------
The trust anchor is local machine access. Anyone who can run this tool already
has read/write access to the SQLite database file (default
``~/.wattracker/wattracker.db``, or wherever ``WATTRACKER_DB`` /
``WATTRACKER_DATA_DIR`` point). Such a person could already overwrite the hash
by hand, so requiring an identity check here would add friction without adding
security -- the tool grants no capability that local file access does not
already confer. It therefore performs no further authentication.

Handling notes:
- The new password is read twice via ``getpass`` (no terminal echo). It is
  never accepted as a CLI argument or environment variable, which would leak it
  into shell history or ``ps`` output.
- Nothing sensitive (no password, no hash) is ever printed; success prints a
  single confirmation line.
- The same database path resolution the app uses (``config.db_path`` /
  ``db.connect``) is honoured, so an env override -- e.g. tests pointing at an
  isolated DB -- is respected.

Sessions: logins are carried in a Starlette ``SessionMiddleware`` signed cookie
holding the user id. Resetting the password does not rotate the session secret
or invalidate issued cookies, so any already-open session for this user stays
valid. For a single-machine local app this is acceptable; the reset still locks
out anyone who only knows the old password, since verification uses the new
hash.
"""
from __future__ import annotations

import getpass
import sys

from . import auth, db


def _read_new_password() -> "str | None":
    """Prompt twice (no echo). Return the password, or None on any rejection."""
    pw1 = getpass.getpass("New password: ")
    pw2 = getpass.getpass("Confirm new password: ")
    if pw1 != pw2:
        print("Error: passwords do not match.", file=sys.stderr)
        return None
    if len(pw1) < auth.MIN_PASSWORD_LEN:
        print(
            f"Error: password must be at least {auth.MIN_PASSWORD_LEN} characters.",
            file=sys.stderr,
        )
        return None
    return pw1


def _list_usernames() -> int:
    names = db.list_usernames()
    if not names:
        print("No users found.")
        return 0
    for name in names:
        print(name)
    return 0


def _reset(username: str) -> int:
    username = (username or "").strip()
    if not username:
        print("Error: username is required.", file=sys.stderr)
        return 2
    if db.get_user_by_username(username) is None:
        print(f"Error: no such user: {username!r}", file=sys.stderr)
        return 1

    new_password = _read_new_password()
    if new_password is None:
        return 2

    if not db.set_password_hash(username, auth.hash_password(new_password)):
        # Raced with a delete between the existence check and the update.
        print(f"Error: no such user: {username!r}", file=sys.stderr)
        return 1

    print(f"Password reset for user {username!r}.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        prog = "python -m wattracker.reset_password"
        print(
            f"Usage:\n  {prog} <username>   reset a user's password\n"
            f"  {prog} --list       list usernames",
            file=sys.stderr,
        )
        return 2
    if args[0] in ("--list", "-l"):
        return _list_usernames()
    return _reset(args[0])


if __name__ == "__main__":
    sys.exit(main())
