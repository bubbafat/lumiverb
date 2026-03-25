"""GET /v1/me — return current user info."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.core.database import get_control_session
from src.repository.control_plane import UserRepository

router = APIRouter(prefix="/v1/me", tags=["me"])


class CurrentUserResponse(BaseModel):
    user_id: str | None
    email: str | None
    role: str


@router.get("", response_model=CurrentUserResponse)
def get_current_user(request: Request) -> CurrentUserResponse:
    """Return the authenticated user's info, or just role for API key auth."""
    user_id: str | None = getattr(request.state, "user_id", None)
    role: str = getattr(request.state, "role", "viewer")

    if not user_id:
        return CurrentUserResponse(user_id=None, email=None, role=role)

    email: str | None = None
    with get_control_session() as session:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is not None:
            email = user.email

    return CurrentUserResponse(user_id=user_id, email=email, role=role)
