"""Mock-аутентификация: пропускает X-User-Id и X-Roles.

В проде здесь была бы валидация JWT от Keycloak через JWKS.
По заданию ЛР4 разрешено замокать Keycloak.
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException

DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ROLES = "CLIENT"


async def get_current_user(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_roles: Optional[str] = Header(None, alias="X-Roles"),
    authorization: Optional[str] = Header(None),
) -> dict:
    user_id = x_user_id or DEFAULT_USER_ID
    roles = [r.strip() for r in (x_roles or DEFAULT_ROLES).split(",") if r.strip()]
    return {"userId": user_id, "roles": roles}


def require_role(role: str):
    async def _checker(user: dict = Depends(get_current_user)) -> dict:
        if role not in user["roles"]:
            raise HTTPException(
                status_code=403,
                detail=f"Role '{role}' required",
            )
        return user

    return _checker
