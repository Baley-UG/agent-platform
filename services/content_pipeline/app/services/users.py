"""Users + project_memberships CRUD."""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core import auth
from app.models.project_memberships import ProjectMembership
from app.models.users import User


def get_by_email(session: Session, email: str) -> Optional[User]:
    return session.exec(select(User).where(User.email == email.lower())).first()


def get(session: Session, user_id: uuid.UUID) -> User:
    row = session.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return row


def list_(session: Session, *, limit: int = 100, offset: int = 0) -> List[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def create(session: Session, *, email: str, password: str, name: Optional[str], role: str) -> User:
    try:
        password_hash = auth.hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    row = User(email=email.lower().strip(), password_hash=password_hash, name=name, role=role)
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already exists") from exc
    session.refresh(row)
    return row


def update(
    session: Session,
    user: User,
    *,
    name: Optional[str] = None,
    role: Optional[str] = None,
    status_: Optional[str] = None,
    password: Optional[str] = None,
) -> User:
    if name is not None:
        user.name = name
    if role is not None:
        user.role = role
    if status_ is not None:
        user.status = status_
    if password is not None:
        try:
            user.password_hash = auth.hash_password(password)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    session.add(user)
    session.flush()
    session.refresh(user)
    return user


def change_password(session: Session, user: User, current: str, new: str) -> User:
    if not auth.verify_password(current, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="current password incorrect")
    return update(session, user, password=new)


def delete(session: Session, user: User) -> None:
    session.delete(user)
    session.flush()


def memberships_for_user(session: Session, user_id: uuid.UUID) -> List[ProjectMembership]:
    stmt = select(ProjectMembership).where(ProjectMembership.user_id == user_id)
    return list(session.exec(stmt).all())


def memberships_for_project(session: Session, project_id: uuid.UUID) -> List[ProjectMembership]:
    stmt = select(ProjectMembership).where(ProjectMembership.project_id == project_id)
    return list(session.exec(stmt).all())


def get_membership(
    session: Session, user_id: uuid.UUID, project_id: uuid.UUID
) -> Optional[ProjectMembership]:
    return session.exec(
        select(ProjectMembership).where(
            ProjectMembership.user_id == user_id,
            ProjectMembership.project_id == project_id,
        )
    ).first()


def add_membership(
    session: Session, user_id: uuid.UUID, project_id: uuid.UUID, role: str
) -> ProjectMembership:
    row = ProjectMembership(user_id=user_id, project_id=project_id, role=role)
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="membership already exists"
        ) from exc
    session.refresh(row)
    return row


def update_membership(session: Session, membership: ProjectMembership, role: str) -> ProjectMembership:
    membership.role = role
    session.add(membership)
    session.flush()
    session.refresh(membership)
    return membership


def remove_membership(session: Session, membership: ProjectMembership) -> None:
    session.delete(membership)
    session.flush()
