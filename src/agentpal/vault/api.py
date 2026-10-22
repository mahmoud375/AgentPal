"""Owner-only Vault controls and an unauthenticated, state-bound browser return."""

from typing import Annotated
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from agentpal.auth.api import OwnerRead, Service, check_request_origin
from agentpal.auth.config import normalized_origin
from agentpal.vault.security import VaultSettings
from agentpal.vault.service import Authority, VaultService


router = APIRouter(prefix="/paypal/vault", tags=["Sandbox Vault onboarding"])


def get_vault_service(request: Request, auth: Service) -> VaultService:
    with request.app.state.auth_lock:
        if request.app.state.vault_service is None:
            try:
                request.app.state.vault_service = VaultService(auth, VaultSettings())
            except ValueError:
                raise HTTPException(503, "Vault unavailable") from None
        return request.app.state.vault_service


Vault = Annotated[VaultService, Depends(get_vault_service)]


async def write_authority(request: Request, auth: Service) -> Authority:
    check_request_origin(request, auth.settings, required=True)
    authority = Authority(request.cookies.get(auth.settings.session_cookie), request.headers.get("x-csrf-token"))
    # Authenticate before reading a body, without holding a lock across upload or I/O.
    with auth.engine.begin() as c:
        owner = auth.session(c, authority.session_token, lock=True)
        if owner is None:
            raise HTTPException(401, "Authentication required")
        if not auth.csrf_matches(owner, authority.csrf):
            raise HTTPException(403, "CSRF validation failed")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16:
            raise HTTPException(400, "Empty Vault request required")
        body.extend(chunk)
    if body.strip() not in (b"", b"{}"):
        raise HTTPException(400, "Empty Vault request required")
    return authority


Write = Annotated[Authority, Depends(write_authority)]


def receipt_cookie(auth) -> str:
    return "__Host-agentpal_vault_return" if auth.settings.secure_cookies else "agentpal_vault_return_dev"


@router.post("/start")
def start(authority: Write, vault: Vault):
    return vault.start(authority)


@router.post("/{onboarding_id}/complete")
def complete(onboarding_id: str, request: Request, authority: Write, vault: Vault):
    try:
        identifier = UUID(onboarding_id)
    except ValueError:
        raise HTTPException(400, "Invalid onboarding identifier") from None
    return vault.complete(authority, identifier, request.cookies.get(receipt_cookie(vault.auth)))


@router.get("/status")
def status(owner: OwnerRead, vault: Vault):
    return vault.status()


@router.get("/returned")
def returned():
    # This query-free JSON landing point has no authority and no frontend code.
    # A future frontend resumes with a fresh same-origin authenticated POST.
    return {"message": "Return to AgentPal and inspect the connection status; completion requires the original owner session"}


def browser_return(request: Request, vault: Vault, *, canceled: bool):
    try:
        if normalized_origin(str(request.url), allow_path=True) != vault.auth.settings.origin:
            raise ValueError
        query = request.scope.pop("vault_callback_query", b"")
        if len(query) > 2048:
            raise ValueError
        params = parse_qsl(query.decode("ascii"), strict_parsing=True, max_num_fields=5)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid PayPal return") from None
    receipt = vault.callback(params, canceled=canceled)
    response = RedirectResponse(vault.auth.settings.origin + "/paypal/vault/returned", status_code=303)
    response.set_cookie(receipt_cookie(vault.auth), receipt, max_age=vault.settings.ttl_seconds,
                        secure=vault.auth.settings.secure_cookies, httponly=True, samesite="strict", path="/")
    return response


@router.get("/return")
def approve_return(request: Request, vault: Vault):
    return browser_return(request, vault, canceled=False)


@router.get("/cancel")
def cancel_return(request: Request, vault: Vault):
    return browser_return(request, vault, canceled=True)
