"""API routes for users and user-assistant bindings."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.assistants.service import AssistantService
from src.shared.auth import require_internal_service_token
from src.users.service import UserService

router = APIRouter(tags=["users"])


def _get_service() -> UserService:
    return UserService()


class CreateUserRequest(BaseModel):
    user_id: str
    display_name: str = ""
    employee_id: str = ""
    department: str = ""
    role: str = "user"


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    employee_id: str | None = None
    department: str | None = None
    role: str | None = None


class BindAssistantRequest(BaseModel):
    assistant_id: str
    alias: str = ""
    source_system: str = ""


class UserResponse(BaseModel):
    user_id: str
    display_name: str
    employee_id: str
    department: str
    role: str
    created_at: datetime
    updated_at: datetime


class BindingResponse(BaseModel):
    user_id: str
    assistant_id: str
    alias: str
    source_system: str
    bound_at: datetime


class BindingListResponse(BaseModel):
    bindings: list[BindingResponse]
    total: int


class UserListResponse(BaseModel):
    users: list[UserResponse]
    total: int


@router.post(
    "/api/v1/users",
    response_model=UserResponse,
    status_code=201,
    dependencies=[Depends(require_internal_service_token)],
)
def create_user(payload: CreateUserRequest, svc: UserService = Depends(_get_service)) -> UserResponse:
    try:
        user = svc.create_user(
            payload.user_id,
            display_name=payload.display_name,
            employee_id=payload.employee_id,
            department=payload.department,
            role=payload.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return UserResponse(**user.model_dump())


@router.get("/api/v1/users", response_model=UserListResponse)
def list_users(svc: UserService = Depends(_get_service)) -> UserListResponse:
    users = [UserResponse(**item.model_dump()) for item in svc.list_users()]
    return UserListResponse(users=users, total=len(users))


@router.get("/api/v1/users/{user_id}", response_model=UserResponse)
def get_user(user_id: str, svc: UserService = Depends(_get_service)) -> UserResponse:
    user = svc.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return UserResponse(**user.model_dump())


@router.put(
    "/api/v1/users/{user_id}",
    response_model=UserResponse,
    dependencies=[Depends(require_internal_service_token)],
)
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    svc: UserService = Depends(_get_service),
) -> UserResponse:
    user = svc.update_user(
        user_id,
        display_name=payload.display_name,
        employee_id=payload.employee_id,
        department=payload.department,
        role=payload.role,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return UserResponse(**user.model_dump())


@router.delete(
    "/api/v1/users/{user_id}",
    status_code=204,
    dependencies=[Depends(require_internal_service_token)],
)
def delete_user(user_id: str, svc: UserService = Depends(_get_service)) -> None:
    if not svc.delete_user(user_id):
        raise HTTPException(status_code=404, detail="user not found")


@router.post(
    "/api/v1/users/{user_id}/assistants/bind",
    response_model=BindingResponse,
    status_code=201,
    dependencies=[Depends(require_internal_service_token)],
)
def bind_assistant(
    user_id: str,
    payload: BindAssistantRequest,
    svc: UserService = Depends(_get_service),
) -> BindingResponse:
    if svc.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        binding = svc.bind_assistant(
            user_id,
            payload.assistant_id,
            alias=payload.alias,
            source_system=payload.source_system,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BindingResponse(**binding.model_dump())


@router.delete(
    "/api/v1/users/{user_id}/assistants/{assistant_id}/unbind",
    status_code=204,
    dependencies=[Depends(require_internal_service_token)],
)
def unbind_assistant(
    user_id: str,
    assistant_id: str,
    svc: UserService = Depends(_get_service),
) -> None:
    if not svc.unbind_assistant(user_id, assistant_id):
        raise HTTPException(status_code=404, detail="binding not found")


@router.get("/api/v1/users/{user_id}/assistants", response_model=BindingListResponse)
def list_user_assistants(
    user_id: str,
    svc: UserService = Depends(_get_service),
) -> BindingListResponse:
    if svc.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    assistant_service = AssistantService()
    bindings = []
    for item in svc.list_bindings_for_user(user_id):
        assistant = assistant_service.get_assistant(item.assistant_id)
        if assistant is None or assistant.deleted:
            continue
        bindings.append(BindingResponse(**item.model_dump()))
    return BindingListResponse(bindings=bindings, total=len(bindings))
