"""
MCC Coverage API
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from scraper import search_plans, get_coverage_string
from sync_nio_ceps import scrape_nio_ceps, write_ceps_csv, write_audit

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
API_KEY = os.getenv("API_KEY", "mcc-n8n-2026-secret")
EXPORT_ROOT = Path(os.getenv("NIO_EXPORT_ROOT", "/tmp/nio_exports"))
EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
NIO_JOBS: dict[str, dict] = {}

app = FastAPI(
    title="MCC Coverage API",
    description="API para verificar cobertura e planos de internet fibra ótica por CEP e número",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def verify_api_key(x_api_key: str | None = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


def _public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in {"task", "csv_path", "audit_path"}}


def _audit_validation(audit: list[dict], total: int) -> tuple[bool, list[str]]:
    problems = []
    for row in audit:
        if row.get("state") in {"PR", "RS"} and row.get("count", 0) >= 29998:
            problems.append(f"{row['state']} atingiu o limite ({row['count']}) sem confirmação de paginação")
    if total == 0:
        problems.append("Nenhum CEP foi coletado")
    return not problems, problems


async def _run_nio_job(job_id: str):
    job = NIO_JOBS[job_id]
    try:
        job.update(status="running", phase="Consultando Power BI regional",
                   started_at=datetime.now(timezone.utc).isoformat())
        audit: list[dict] = []
        ceps = await scrape_nio_ceps(audit)
        job.update(phase="Validando CEPs e auditoria", collected=len(ceps))
        folder = EXPORT_ROOT / job_id
        folder.mkdir(parents=True, exist_ok=True)
        csv_path = folder / "ceps_nio_nacional.csv"
        audit_path = folder / "ceps_nio_auditoria.json"
        write_ceps_csv(ceps, str(csv_path))
        write_audit(audit, str(audit_path))
        valid, problems = _audit_validation(audit, len(ceps))
        job.update(status="completed", phase="Pronto para download" if valid else "Concluído com pendências",
                   valid=valid, problems=problems, csv_path=str(csv_path), audit_path=str(audit_path),
                   finished_at=datetime.now(timezone.utc).isoformat())
    except asyncio.CancelledError:
        job.update(status="cancelled", phase="Cancelado")
        raise
    except Exception as exc:
        job.update(status="failed", phase="Falha", error=str(exc),
                   finished_at=datetime.now(timezone.utc).isoformat())


@app.post("/api/nio/export/start", dependencies=[Depends(verify_api_key)])
async def start_nio_export():
    active = next((job for job in NIO_JOBS.values() if job.get("status") in {"queued", "running"}), None)
    if active:
        return _public_job(active)
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "status": "queued", "phase": "Aguardando execução", "collected": 0,
           "valid": False, "problems": [], "created_at": datetime.now(timezone.utc).isoformat()}
    NIO_JOBS[job_id] = job
    job["task"] = asyncio.create_task(_run_nio_job(job_id))
    return _public_job(job)


@app.get("/api/nio/export/status", dependencies=[Depends(verify_api_key)])
async def nio_export_status(job_id: str | None = Query(None)):
    if job_id:
        job = NIO_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")
        return _public_job(job)
    return {"jobs": [_public_job(job) for job in list(NIO_JOBS.values())[-10:]]}


@app.post("/api/nio/export/cancel", dependencies=[Depends(verify_api_key)])
async def cancel_nio_export(payload: dict):
    job_id = str(payload.get("job_id", ""))
    job = NIO_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    task = job.get("task")
    if task and not task.done():
        task.cancel()
    job.update(status="cancelled", phase="Cancelado")
    return _public_job(job)


@app.get("/api/nio/export/download", dependencies=[Depends(verify_api_key)])
async def download_nio_export(kind: str, job_id: str):
    job = NIO_JOBS.get(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=404, detail="Exportação não concluída")
    if kind == "csv":
        path, filename = job.get("csv_path"), "ceps_nio_nacional.csv"
    elif kind == "audit":
        path, filename = job.get("audit_path"), "ceps_nio_auditoria.json"
    else:
        raise HTTPException(status_code=400, detail="Tipo de arquivo inválido")
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    media_type = "text/csv" if kind == "csv" else "application/json"
    return FileResponse(path, filename=filename, media_type=media_type)


class StreamingService(BaseModel):
    name: str
    logo: str


class Plan(BaseModel):
    id: str
    rankingPosition: int | None
    providerName: str
    providerSlug: str
    providerLogo: str
    planName: str
    promoted: bool
    badges: list[str]
    downloadSpeed: int | None
    downloadLabel: str
    uploadSpeed: int | None
    price: float | None
    priceLabel: str
    priceExtraInfo: list[str]
    setupFee: str
    totalAnnualPrice: float | None
    technology: str
    technologyValue: str
    contractDuration: int | None
    breakFee: float | None
    providerRating: float | None
    ratingCount: int | None
    streamingServices: list[StreamingService]


class Address(BaseModel):
    street: str
    neighborhood: str
    city: str
    state: str


class Location(BaseModel):
    city: str
    state: str
    locationId: str


class SearchResponse(BaseModel):
    cep: str
    number: str
    address: Address
    location: Location
    totalPlans: int
    plans: list[Plan]
    nioCoverage: bool = False


@app.get(
    "/api/plans",
    response_model=SearchResponse,
    summary="Buscar planos de internet por CEP",
    description="Retorna os planos de internet fibra ótica disponíveis para o endereço informado.",
    dependencies=[Depends(verify_api_key)],
)
def get_plans(
    cep: str = Query(..., description="CEP (8 dígitos)", examples=["15014050"], min_length=8, max_length=9),
    number: str = Query(..., description="Número da residência", examples=["3495"]),
):
    result = search_plans(cep, number)

    if "error" in result:
        error = result["error"]
        status = 400 if "dígitos" in error else 404
        raise HTTPException(status_code=status, detail=error)

    return result


class CoverageResponse(BaseModel):
    cep: str
    number: str
    coverage: str


@app.get(
    "/api/coverage",
    response_model=CoverageResponse,
    summary="Cobertura disponível por CEP",
    description="Retorna os provedores com cobertura no endereço informado.",
    dependencies=[Depends(verify_api_key)],
)
def get_coverage(
    cep: str = Query(..., description="CEP (8 dígitos)", examples=["23013620"], min_length=8, max_length=9),
    number: str = Query(..., description="Número da residência", examples=["123"]),
):
    cep_clean = cep.replace("-", "").strip()
    return CoverageResponse(
        cep=cep_clean,
        number=number,
        coverage=get_coverage_string(cep_clean, number),
    )


@app.get("/health")
def health():
    return {"status": "ok"}

