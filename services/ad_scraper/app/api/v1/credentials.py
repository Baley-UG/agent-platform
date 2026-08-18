"""YouCloud session-token management.

The token is the whole auth surface. Rotate it by pasting a fresh
`sessionId` cookie from a logged-in browser:

    PUT /credentials/session  {"session_cookie": "<sessionId>"}

There is no login endpoint. Automatic login was considered and dropped —
see `app/services/credentials.py` for the reasoning. No endpoint here ever
returns the token.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.v1.deps import get_session, require_api_key
from app.schemas.credentials import CredentialRead, SessionUpdate
from app.services import credentials as creds

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("", response_model=CredentialRead)
def read_credential(session: Session = Depends(get_session)) -> CredentialRead:
    """Current token state. Lazily creates an empty row on first call."""
    row = creds.get_or_create(session)
    return CredentialRead(**creds.redacted_view(row))


@router.put("/session", response_model=CredentialRead)
def update_session(payload: SessionUpdate, session: Session = Depends(get_session)) -> CredentialRead:
    """Store a `sessionId` token captured from a logged-in browser.

    The expiry is read from the token's own `exp` claim, and the in-process
    cache is primed so the next job skips the DB. A token whose expiry can't
    be parsed is still accepted — it may be perfectly valid — but
    `session_expires_at` comes back null and we can't warn ahead of its
    death; the first rejection becomes the signal instead.
    """
    try:
        row = creds.set_session_cookie(session, payload.session_cookie)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Fernet key missing / malformed — operator misconfiguration, not a
        # bad request.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return CredentialRead(**creds.redacted_view(row))


@router.post("/session/invalidate-cache", response_model=CredentialRead)
def invalidate_cache(session: Session = Depends(get_session)) -> CredentialRead:
    """Drop the in-process token cache without changing the stored token.

    Only needed when the row was edited out of band (a direct SQL update, or
    another replica storing a newer token) — the normal paths invalidate it
    on their own.
    """
    creds.invalidate_cache("api_request")
    return CredentialRead(**creds.redacted_view(creds.get_credential(session)))


@router.post("/disable", response_model=CredentialRead)
def disable_credential(session: Session = Depends(get_session)) -> CredentialRead:
    """Take the credential out of service without deleting its token."""
    row = creds.disable(session)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no credential row")
    return CredentialRead(**creds.redacted_view(row))
