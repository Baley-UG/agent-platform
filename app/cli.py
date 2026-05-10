"""Admin operations CLI for the main app.

Usage (inside the running container):

    docker compose exec app python -m app.cli create-admin --email admin@example.com
    docker compose exec app python -m app.cli list-admins
    docker compose exec app python -m app.cli set-role --email user@example.com --role admin

`create-admin` prompts for the password interactively (no shell history
trail). For scripted use, pass `--password ...` explicitly.

Adding a subcommand: drop a function `cmd_<name>(args, session)` below
and register it in `_SUBCOMMANDS`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Callable, Dict

from sqlmodel import Session, select

from app.models.user import User
from app.services import admin_auth_service as svc
from app.services.database import database_service


# ---------- subcommands ----------


def cmd_create_admin(args: argparse.Namespace, session: Session) -> int:
    email = args.email.strip().lower()
    name = args.name or email.split("@", 1)[0]
    role = "admin"

    existing = svc.get_user_by_email(session, email)
    if existing is not None:
        print(
            f"error: user with email '{email}' already exists (role={existing.role}, "
            f"status={existing.status}).",
            file=sys.stderr,
        )
        if existing.role != "admin":
            print(
                f"hint: upgrade with `python -m app.cli set-role --email {email} --role admin`",
                file=sys.stderr,
            )
        return 2

    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("error: passwords don't match", file=sys.stderr)
            return 2
    if len(password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 2

    user = svc.create_user(session, email=email, password=password, name=name, role=role)
    print(f"ok: created admin id={user.id} email={user.email} name={user.name}")
    return 0


def cmd_list_admins(args: argparse.Namespace, session: Session) -> int:  # noqa: ARG001
    rows = list(session.exec(select(User).where(User.role == "admin").order_by(User.created_at)).all())
    if not rows:
        print("(no admin users)")
        return 0
    print(f"{'id':>5}  {'email':<40}  {'status':<10}  {'last_login':<25}  name")
    print("-" * 110)
    for u in rows:
        last = u.last_login_at.isoformat(timespec="seconds") if u.last_login_at else "-"
        print(f"{u.id:>5}  {u.email:<40}  {u.status:<10}  {last:<25}  {u.name or ''}")
    return 0


def cmd_set_role(args: argparse.Namespace, session: Session) -> int:
    email = args.email.strip().lower()
    if args.role not in ("admin", "member"):
        print(f"error: role must be 'admin' or 'member' (got '{args.role}')", file=sys.stderr)
        return 2
    user = svc.get_user_by_email(session, email)
    if user is None:
        print(f"error: no user with email '{email}'", file=sys.stderr)
        return 2
    if user.role == args.role:
        print(f"ok: user {email} already has role={args.role}")
        return 0
    user = svc.update_user(session, user, role=args.role)
    print(f"ok: user {email} role changed to {user.role}")
    return 0


def cmd_disable_user(args: argparse.Namespace, session: Session) -> int:
    user = svc.get_user_by_email(session, args.email.strip().lower())
    if user is None:
        print(f"error: no user with email '{args.email}'", file=sys.stderr)
        return 2
    svc.update_user(session, user, status_="disabled")
    print(f"ok: user {user.email} disabled")
    return 0


def cmd_enable_user(args: argparse.Namespace, session: Session) -> int:
    user = svc.get_user_by_email(session, args.email.strip().lower())
    if user is None:
        print(f"error: no user with email '{args.email}'", file=sys.stderr)
        return 2
    svc.update_user(session, user, status_="active")
    print(f"ok: user {user.email} enabled")
    return 0


def cmd_reset_password(args: argparse.Namespace, session: Session) -> int:
    user = svc.get_user_by_email(session, args.email.strip().lower())
    if user is None:
        print(f"error: no user with email '{args.email}'", file=sys.stderr)
        return 2
    password = args.password
    if password is None:
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("error: passwords don't match", file=sys.stderr)
            return 2
    if len(password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 2
    svc.update_user(session, user, password=password)
    print(f"ok: password reset for {user.email}")
    return 0


_SUBCOMMANDS: Dict[str, Callable[[argparse.Namespace, Session], int]] = {
    "create-admin": cmd_create_admin,
    "list-admins": cmd_list_admins,
    "set-role": cmd_set_role,
    "disable-user": cmd_disable_user,
    "enable-user": cmd_enable_user,
    "reset-password": cmd_reset_password,
}


# ---------- argparse wiring ----------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Admin operations on the main app's user table.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="Create a new admin user (prompts for password).")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--password", default=None, help="Optional. If omitted, prompts interactively.")

    sub.add_parser("list-admins", help="List all users with role='admin'.")

    p = sub.add_parser("set-role", help="Change a user's global role.")
    p.add_argument("--email", required=True)
    p.add_argument("--role", required=True, choices=["admin", "member"])

    p = sub.add_parser("disable-user", help="Mark a user status='disabled'.")
    p.add_argument("--email", required=True)

    p = sub.add_parser("enable-user", help="Mark a user status='active'.")
    p.add_argument("--email", required=True)

    p = sub.add_parser("reset-password", help="Set a new password for a user (prompts).")
    p.add_argument("--email", required=True)
    p.add_argument("--password", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _SUBCOMMANDS[args.cmd]
    with Session(database_service.engine) as session:
        return handler(args, session)


if __name__ == "__main__":
    sys.exit(main())
