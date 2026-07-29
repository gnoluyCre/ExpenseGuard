"""Policy family, immutable document, candidate, and binding API."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthDep, SettingsDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.policies.bindings import BindingSelection, save_rule_policy_bindings
from app.core.policies.candidates import BindingCandidate, search_binding_candidates
from app.core.policies.models import PolicyLimits
from app.core.policies.query_service import (
    BindingHistoryView,
    PolicyDocumentView,
    PolicyFamilyListItem,
    binding_query_for_rule,
    get_active_generation,
    get_policy_document,
    list_policy_families,
    list_rule_bindings,
)
from app.core.policies.service import (
    IndexProfile,
    create_policy_family,
    ensure_initial_active_generation,
    publish_policy_document,
    upload_policy_document,
)
from app.core.policies.storage import PrivatePolicyStorage
from app.core.retrieval import build_local_retrieval
from app.core.security.permissions import Permission
from app.settings import Settings

router = APIRouter(tags=["policies"])


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePolicyFamilyRequest(_StrictRequest):
    stable_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=512)


class CreatePolicyFamilyResponse(BaseModel):
    family: PolicyFamilyListItem
    reused_existing: bool


class UploadPolicyResponse(BaseModel):
    document: PolicyDocumentView
    reused_existing: bool
    clause_count: int = Field(ge=1)
    chunk_count: int = Field(ge=1)


class PublishPolicyResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    reused_existing: bool


class SaveBindingsRequest(_StrictRequest):
    expense_date: date
    selections: tuple[BindingSelection, ...] = Field(min_length=1, max_length=3)


class SaveBindingsResponse(BaseModel):
    items: tuple[BindingHistoryView, ...]
    reused_existing: bool


_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/api/policies/families",
    response_model=tuple[PolicyFamilyListItem, ...],
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="list_families",
)
async def list_policy_families_endpoint(db: TenantDbDep) -> tuple[PolicyFamilyListItem, ...]:
    return await list_policy_families(db)


@router.post(
    "/api/policies/families",
    response_model=CreatePolicyFamilyResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": CreatePolicyFamilyResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="create_family",
)
async def create_policy_family_endpoint(
    payload: CreatePolicyFamilyRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> CreatePolicyFamilyResponse:
    family, created = await create_policy_family(
        db,
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        stable_key=payload.stable_key,
        display_name=payload.display_name,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    listed = await list_policy_families(db)
    family_view = next(item for item in listed if item.id == family.id)
    return CreatePolicyFamilyResponse(family=family_view, reused_existing=not created)


@router.post(
    "/api/policies/documents",
    response_model=UploadPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": UploadPolicyResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="upload_document",
)
async def upload_policy_document_endpoint(
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    settings: SettingsDep,
    family_id: Annotated[uuid.UUID, Form()],
    title: Annotated[str, Form(min_length=1, max_length=512)],
    version: Annotated[str, Form(min_length=1, max_length=64)],
    effective_date: Annotated[date, Form()],
    file: Annotated[UploadFile, File()],
    expiry_date: Annotated[date | None, Form()] = None,
) -> UploadPolicyResponse:
    content = await file.read()
    result = await upload_policy_document(
        db,
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        family_id=family_id,
        title=title,
        version=version,
        effective_date=effective_date,
        expiry_date=expiry_date,
        filename=file.filename or "",
        content=content,
        limits=_policy_limits(settings),
        storage=PrivatePolicyStorage(settings.policy_private_storage_root),
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return UploadPolicyResponse(
        document=await get_policy_document(db, result.document.id),
        reused_existing=not result.created,
        clause_count=len(result.parsed.clauses),
        chunk_count=len(result.parsed.chunks),
    )


@router.get(
    "/api/policies/documents/{document_id}",
    response_model=PolicyDocumentView,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="get_document",
)
async def get_policy_document_endpoint(
    document_id: uuid.UUID, db: TenantDbDep
) -> PolicyDocumentView:
    return await get_policy_document(db, document_id)


@router.post(
    "/api/policies/documents/{document_id}/publish",
    response_model=PublishPolicyResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_200_OK: {"model": PublishPolicyResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="publish_document",
)
async def publish_policy_document_endpoint(
    document_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    settings: SettingsDep,
) -> PublishPolicyResponse:
    await ensure_initial_active_generation(
        db,
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        profile=_index_profile(settings),
    )
    document = await publish_policy_document(
        db,
        tenant_id=auth.tenant_id,
        published_by=auth.user_id,
        document_id=document_id,
        attempt_limit=settings.policy_index_attempt_limit,
    )
    reused = document.status.value == "published"
    response.status_code = status.HTTP_200_OK if reused else status.HTTP_202_ACCEPTED
    return PublishPolicyResponse(
        document_id=document.id,
        status=document.status.value,
        reused_existing=reused,
    )


@router.get(
    "/api/rules/{rule_config_id}/policy-candidates",
    response_model=tuple[BindingCandidate, ...],
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="rule_candidates",
)
async def rule_policy_candidates_endpoint(
    rule_config_id: uuid.UUID,
    expense_date: Annotated[date, Query()],
    db: TenantDbDep,
    auth: AuthDep,
    settings: SettingsDep,
) -> tuple[BindingCandidate, ...]:
    generation = await get_active_generation(db)
    vector_store, reranker = build_local_retrieval(settings, generation)
    try:
        return await search_binding_candidates(
            db,
            tenant_id=auth.tenant_id,
            expense_date=expense_date,
            query=await binding_query_for_rule(db, rule_config_id),
            vector_store=vector_store,
            reranker=reranker,
            top_k=settings.policy_candidate_top_k,
            cutoff=settings.policy_candidate_cutoff,
        )
    finally:
        await vector_store.close()


@router.post(
    "/api/rules/{rule_config_id}/policy-bindings",
    response_model=SaveBindingsResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": SaveBindingsResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="save_rule_bindings",
)
async def save_rule_bindings_endpoint(
    rule_config_id: uuid.UUID,
    payload: SaveBindingsRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> SaveBindingsResponse:
    result = await save_rule_policy_bindings(
        db,
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        rule_config_id=rule_config_id,
        expense_date=payload.expense_date,
        selections=payload.selections,
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    ids = {item.id for item in result.bindings}
    history = await list_rule_bindings(db, rule_config_id)
    return SaveBindingsResponse(
        items=tuple(item for item in history if item.id in ids),
        reused_existing=not result.created,
    )


@router.get(
    "/api/rules/{rule_config_id}/policy-bindings",
    response_model=tuple[BindingHistoryView, ...],
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="list_rule_bindings",
)
async def list_rule_bindings_endpoint(
    rule_config_id: uuid.UUID, db: TenantDbDep
) -> tuple[BindingHistoryView, ...]:
    return await list_rule_bindings(db, rule_config_id)


def _policy_limits(settings: Settings) -> PolicyLimits:
    return PolicyLimits(
        max_file_bytes=settings.policy_max_file_bytes,
        max_pdf_pages=settings.policy_max_pdf_pages,
        max_extracted_chars=settings.policy_max_extracted_chars,
        max_clauses=settings.policy_max_clauses,
        chunk_chars=settings.policy_chunk_chars,
    )


def _index_profile(settings: Settings) -> IndexProfile:
    return IndexProfile(
        collection_name=settings.qdrant_collection,
        collection_alias=settings.qdrant_collection,
        vector_size=settings.policy_embedding_vector_size,
        embedding_model_family="local",
        embedding_model_id=settings.policy_embedding_model,
        embedding_model_revision=settings.policy_embedding_revision,
        rerank_model_family="local",
        rerank_model_id=settings.policy_rerank_model,
        rerank_model_revision=settings.policy_rerank_revision,
    )
