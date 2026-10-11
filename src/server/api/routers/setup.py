"""First-run setup from the web app (src/server/setup.py): the first admin,
while nobody can log in yet, and only from the machine Lumiverb runs on. A
new install is set up on that machine before it's opened to others; this
holds for one that is open already (the brain's nginx listens everywhere)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.server.accounts import AccountError
from src.server.api.client_address import on_this_machine
from src.server.api.errors import ApiError
from src.server.api.routers.auth import LoginResponse, _issue_jwt
from src.server.config import get_settings
from src.server.database import get_control_session
from src.server.repository.control_plane import UserRepository
from src.server.setup import SetupError, create_first_admin, setup_needed

router = APIRouter(prefix="/v1/setup", tags=["setup"])

ELSEWHERE = "Open Lumiverb on the computer it's installed on to finish setting up."


class SetupStatus(BaseModel):
    needed: bool  # nobody can log in yet
    on_this_machine: bool  # this request may set it up


class SetupRequest(BaseModel):
    email: str
    password: str
    account_name: str | None = None  # a new account's; "Lumiverb" when not given


@router.get("", response_model=SetupStatus)
def setup_status(request: Request) -> SetupStatus:
    with get_control_session() as session:
        needed = setup_needed(session)
    return SetupStatus(needed=needed, on_this_machine=on_this_machine(request))


@router.post("", response_model=LoginResponse, status_code=201)
def set_up(request: Request, body: SetupRequest) -> LoginResponse:
    """Makes the first admin and logs them in."""
    if not on_this_machine(request):
        raise ApiError(403, "setup_elsewhere", ELSEWHERE)
    jwt_secret = get_settings().jwt_secret
    if not jwt_secret:
        raise ApiError(500, "jwt_secret_missing", "JWT_SECRET isn't set in the install's env file.")
    try:
        user = create_first_admin(body.email, body.password, account_name=body.account_name)
    except SetupError as e:
        raise ApiError(e.status, e.code, e.message) from e
    except AccountError as e:
        raise ApiError(500, "account_failed", str(e)) from e
    with get_control_session() as session:
        UserRepository(session).update_last_login(user.user_id)
    return LoginResponse(access_token=_issue_jwt(user, jwt_secret))
