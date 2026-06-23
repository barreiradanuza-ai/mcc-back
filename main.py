"""
MCC Coverage API
"""

import os

from fastapi import Depends, FastAPI, Header, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from scraper import search_plans, get_coverage_string

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
API_KEY = os.getenv("API_KEY", "mcc-n8n-2026-secret")

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
