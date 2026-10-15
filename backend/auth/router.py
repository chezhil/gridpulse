"""
GridPulse Operator Authentication & RBAC Module.
Provides session tokens, demo operator bypass, and permission enforcement
for SCADA / GridPulse state-changing API routes.
"""

import uuid
import secrets
from typing import Optional, Dict
from fastapi import APIRouter, HTTPException, Depends, Header, status
from pydantic import BaseModel, Field

auth_router = APIRouter()

# In-memory session cache for hackathon MVP
SESSIONS: Dict[str, "OperatorSession"] = {}


class LoginRequest(BaseModel):
    username: Optional[str] = Field(None, description="Operator handle or ID")
    password: Optional[str] = Field(None, description="Operator credentials or PIN")
    role: Optional[str] = Field("operator", description="Demo role: operator, engineer, or auditor")
    demo: bool = False
    is_demo_bypass: Optional[bool] = Field(False, description="Flag for instant hackathon judge bypass")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    operator_id: str
    role: str
    session_badge: str
    is_demo: bool
    message: str


class OperatorSession(BaseModel):
    token: str
    operator_id: str
    role: str
    session_badge: str
    is_demo: bool


@auth_router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    """
    POST /api/auth/login
    Authenticates a SCADA operator or initiates a demo session.
    Clearly marked as hackathon demonstration access; not production NERC CIP certified.
    """
    # Demo Bypass condition: explicitly requested, or demo credentials, or empty fields
    is_demo = req.demo or req.is_demo_bypass or (
        req.username in (None, "", "demo", "operator", "OP_NERC_772") and
        req.password in (None, "", "demo", "password")
    )

    if not is_demo and req.password != "gridpulse2026":
        # Check simple operator authorization for non-demo logins
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator credentials. Use Demo Bypass or password 'gridpulse2026'."
        )

    aliases = {"NERC_OPERATOR": "operator", "SCADA_ADMIN": "operator", "VIEWER": "auditor"}
    requested = req.role or "operator"
    role = aliases.get(requested.upper(), requested.lower())
    if role not in ("operator", "engineer", "auditor"):
        raise HTTPException(status_code=422, detail="Unknown demo role")

    operator_id = req.username if (req.username and req.username.strip()) else "OP_NERC_772"
    token = f"gp-{secrets.token_hex(16)}"
    badge = f"NERC-CIP-005-DEMO-{role}"

    session = OperatorSession(
        token=token,
        operator_id=operator_id,
        role=role,
        session_badge=badge,
        is_demo=is_demo
    )
    SESSIONS[token] = session

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        operator_id=operator_id,
        role=role,
        session_badge=badge,
        is_demo=is_demo,
        message="Demo Operator authenticated successfully (Simulation Mode)." if is_demo else "Operator session active."
    )


def verify_operator_token(
    authorization: Optional[str] = Header(None, description="Bearer token header"),
    x_operator_token: Optional[str] = Header(None, alias="X-Operator-Token")
) -> OperatorSession:
    """
    Dependency to enforce operator authentication on state-changing grid endpoints.
    Allows header 'Authorization: Bearer <token>' or 'X-Operator-Token: <token>'.
    Rejects missing or invalid sessions.
    """
    token = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]
    elif x_operator_token:
        token = x_operator_token

    if not token or token not in SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Valid SCADA Operator token required. Please login via /api/auth/login.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    session = SESSIONS[token]
    return session


def require_role(allowed_roles: list[str]):
    """Enforces specific RBAC roles for mutating actions."""
    def role_checker(session: OperatorSession = Depends(verify_operator_token)) -> OperatorSession:
        if session.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Operator role '{session.role}' lacks permission for this action. Required: {allowed_roles}"
            )
        return session
    return role_checker


@auth_router.get('/session')
def session_info(session: OperatorSession = Depends(verify_operator_token)):
    """Validate an in-memory demo session without returning its token."""
    return session.model_dump(exclude={'token'})
