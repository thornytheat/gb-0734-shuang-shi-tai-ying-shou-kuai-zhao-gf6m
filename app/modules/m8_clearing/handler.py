from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from app.config import MAX_BYTES
from app.modules.m8_clearing import service
from app.modules.m8_clearing.engine import POLICIES
from app.store import Store, get_store

router = APIRouter(prefix="/api/clearing", tags=["回款核销"])


class ExplanationRequest(BaseModel):
    effective: str
    known: str
    note: str


@router.post("/batches")
def upload_batch(file: UploadFile = File(...), source_id: str = Form(...),
                 policy: str = Form("SWEEP"), effective: str = Form(...),
                 known: str = Form(...), store: Store = Depends(get_store)):
    if policy not in POLICIES:
        from app.errors import Problem
        raise Problem("INVALID_POLICY", f"裁决策略须为 {POLICIES} 之一")
    return service.ingest_batch(store, file.file.read(MAX_BYTES + 1), source_id, policy,
                                effective, known)


@router.get("")
def overview(source_id: str, effective: str, known: str, store: Store = Depends(get_store)):
    return service.overview(store, source_id, effective, known)


@router.get("/close")
def close(source_id: str, effective: str, known: str, store: Store = Depends(get_store)):
    return service.close_guard(store, source_id, effective, known)


@router.post("/explanations")
def explain(request: ExplanationRequest, store: Store = Depends(get_store)):
    return service.record_explanation(store, request.effective, request.known, request.note)


@router.post("/prepay-sweep")
def prepay_sweep(source_id: str, effective: str, known: str, store: Store = Depends(get_store)):
    return service.sweep_prepayments(store, source_id, effective, known)


@router.get("/batches/{batch_id}")
def detail(batch_id: str, store: Store = Depends(get_store)):
    return service.batch_detail(store, batch_id)
