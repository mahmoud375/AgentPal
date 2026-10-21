"""Cookie-only owner routes and reusable read/write authorization dependencies."""

from collections.abc import Iterator
from datetime import timezone
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agentpal.auth.config import AuthSettings, normalized_origin
from agentpal.auth.service import AuthService, LoginThrottled, NewSession, OwnerSession, OwnerWriteContext
from agentpal.db import DatabaseSettings, create_database_engine


router = APIRouter(prefix="/auth", tags=["owner authentication"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    login_name: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=128, repr=False)


class OwnerInfo(BaseModel):
    login_name: str


def get_auth_service(request: Request) -> AuthService:
    # Preserve the config-free health endpoint and import; auth fails closed if
    # explicit configuration is missing. No dotenv file is opened.
    with request.app.state.auth_lock:
        if request.app.state.auth_service is None:
            try:
                settings = AuthSettings()
                engine = create_database_engine(DatabaseSettings())
            except ValueError:
                raise HTTPException(503, "Authentication unavailable") from None
            request.app.state.auth_service = AuthService(engine, settings)
            request.app.state.auth_owned_engine = True
        return request.app.state.auth_service


Service = Annotated[AuthService, Depends(get_auth_service)]


def check_request_origin(request: Request, settings: AuthSettings, *, required: bool) -> None:
    try:
        if normalized_origin(str(request.url), allow_path=True) != settings.origin:
            raise ValueError
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            raise ValueError
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin is not None:
            if normalized_origin(origin) != settings.origin:
                raise ValueError
        elif referer:
            if normalized_origin(referer, allow_path=True) != settings.origin:
                raise ValueError
        elif required:
            raise ValueError
    except ValueError:
        raise HTTPException(403, "Request origin rejected") from None


async def read_login(request: Request, service: Service) -> LoginRequest:
    check_request_origin(request, service.settings, required=True)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON authentication request required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, "Authentication request too large")
        body.extend(chunk)
    try:
        return LoginRequest.model_validate(json.loads(body))
    except (ValueError, ValidationError, RecursionError):
        # FastAPI's normal validation response can echo the submitted password.
        raise HTTPException(400, "Invalid authentication request") from None


def set_session_cookies(response: Response, service: AuthService, session: NewSession) -> None:
    common = dict(max_age=service.settings.session_ttl_seconds, expires=session.expires_at.astimezone(timezone.utc),
                  path="/", secure=service.settings.secure_cookies, samesite="strict")
    response.set_cookie(service.settings.session_cookie, session.token, httponly=True, **common)
    # Only this purpose-specific token is readable by same-origin frontend code.
    response.set_cookie(service.settings.csrf_cookie, session.csrf, httponly=False, **common)


def clear_session_cookies(response: Response, settings: AuthSettings) -> None:
    response.delete_cookie(settings.session_cookie, path="/", secure=settings.secure_cookies,
                           httponly=True, samesite="strict")
    response.delete_cookie(settings.csrf_cookie, path="/", secure=settings.secure_cookies,
                           httponly=False, samesite="strict")


@router.post("/login", response_model=OwnerInfo, openapi_extra={"requestBody": {
    "required": True, "content": {"application/json": {"schema": LoginRequest.model_json_schema()}}}})
def login(response: Response, request: Request, service: Service,
          payload: Annotated[LoginRequest, Depends(read_login)]) -> OwnerInfo:
    try:
        session = service.login(payload.login_name, payload.password.get_secret_value(),
                                request.cookies.get(service.settings.session_cookie))
    except LoginThrottled as error:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(error.retry_after)}) from None
    if session is None:
        raise HTTPException(401, "Invalid credentials")
    set_session_cookies(response, service, session)
    return OwnerInfo(login_name=session.login_name)


def require_owner(request: Request, response: Response, service: Service) -> OwnerSession:
    check_request_origin(request, service.settings, required=False)
    with service.engine.connect() as c:
        session = service.session(c, request.cookies.get(service.settings.session_cookie))
    if session is None:
        raise HTTPException(401, "Authentication required")
    response.headers["Cache-Control"] = "no-store"
    return session


OwnerRead = Annotated[OwnerSession, Depends(require_owner)]


def require_owner_write(request: Request, response: Response, service: Service) -> Iterator[OwnerWriteContext]:
    check_request_origin(request, service.settings, required=True)
    with service.engine.begin() as c:
        session = service.session(c, request.cookies.get(service.settings.session_cookie), lock=True)
        if session is None:
            raise HTTPException(401, "Authentication required")
        if not service.csrf_matches(session, request.headers.get("x-csrf-token")):
            raise HTTPException(403, "CSRF validation failed")
        response.headers["Cache-Control"] = "no-store"
        # Future financial services must use this connection and this verified
        # session ID, never a caller-supplied owner/approver ID or another session.
        yield OwnerWriteContext(session, c)


# Finalize/commit the transaction before sending the response.
OwnerWrite = Annotated[OwnerWriteContext, Depends(require_owner_write, scope="function")]


@router.get("/me", response_model=OwnerInfo)
def current_owner(owner: OwnerRead) -> OwnerInfo:
    return OwnerInfo(login_name=owner.login_name)


@router.post("/logout", status_code=204)
def logout(request: Request, service: Service) -> Response:
    check_request_origin(request, service.settings, required=True)
    token = request.cookies.get(service.settings.session_cookie)
    with service.engine.begin() as c:
        session = service.session(c, token, lock=True)
        if session:
            if not service.csrf_matches(session, request.headers.get("x-csrf-token")):
                raise HTTPException(403, "CSRF validation failed")
            service.revoke(c, token)
        # Missing, expired and already revoked sessions have no authority; clear
        # stale cookies predictably without granting an authenticated operation.
    response = Response(status_code=204)
    clear_session_cookies(response, service.settings)
    return response
