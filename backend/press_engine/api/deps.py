"""Request dependencies: DB session, services, and token auth."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User

COOKIE = "pe_token"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "pe_" + secrets.token_urlsafe(32)


def create_user(session: Session, name: str, *, role: str = "member", email: str | None = None) -> tuple[User, str]:
    token = new_token()
    user = User(name=name, role=role, email=email, token_hash=hash_token(token), notify_replies=(role == "owner"))
    session.add(user)
    session.flush()
    return user, token


def rotate_token(session: Session, user: User) -> str:
    token = new_token()
    user.token_hash = hash_token(token)
    session.flush()
    return token


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.db.session() as s:
        yield s


def get_svc(request: Request):
    return request.app.state.svc


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE)


def user_for_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    return session.scalar(select(User).where(User.token_hash == hash_token(token)))


def api_user(request: Request, session: Session = Depends(get_db)) -> User:
    user = user_for_token(session, _token_from(request))
    if user is None:
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    return user


def api_owner(user: User = Depends(api_user)) -> User:
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="Only the owner can do this")
    return user


class LoginRequired(Exception):
    pass


class OwnerRequired(Exception):
    pass


def session_user(request: Request, session: Session) -> User | None:
    """Dashboard login: a signed session cookie, or (older logins) the raw token cookie."""
    from ..sessions import COOKIE as SESSION_COOKIE
    from ..sessions import verify

    user_id = verify(request.app.state.svc.cfg.data_dir, request.cookies.get(SESSION_COOKIE))
    if user_id is not None:
        user = session.get(User, user_id)
        if user is not None:
            return user
    return user_for_token(session, request.cookies.get(COOKIE))


def web_user(request: Request, session: Session = Depends(get_db)) -> User:
    user = session_user(request, session)
    if user is None:
        raise LoginRequired()
    return user


def web_owner(user: User = Depends(web_user)) -> User:
    if not user.is_owner:
        raise OwnerRequired()
    return user
