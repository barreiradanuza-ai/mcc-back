"""Standalone live NIO facade lookup service.

This module is deployed as a separate Railway service with:
    uvicorn nio_live_service:app --host 0.0.0.0 --port $PORT

It does not read or write the MCC database. It keeps one Playwright page per
regional Power BI report and queries only the requested CEP.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from sync_nio_ceps import (
    BRAZIL_STATES,
    HEADLESS,
    POWERBI_URLS,
    _clear_report_filters,
    _login,
    _select_state,
)
from extract_nio_facades import FIELDS, _decode_dsr_cell, _value_dicts
from playwright.async_api import Browser, Page, async_playwright

API_KEY = os.getenv("NIO_LIVE_API_KEY", os.getenv("API_KEY", ""))
REGIONS = ("Centro Oeste", "Norte e Nordeste", "Sudeste", "Sul", "Sao Paulo")
STATE_TO_REGION = {
    **dict.fromkeys("DF GO MS MT".split(), "Centro Oeste"),
    **dict.fromkeys("AC AL AP AM BA CE MA PA PB PE RN RO RR TO".split(), "Norte e Nordeste"),
    **dict.fromkeys("ES MG RJ".split(), "Sudeste"),
    **dict.fromkeys("PR RS SC".split(), "Sul"),
    "SP": "Sao Paulo",
}
CEP_RE = re.compile(r"^\d{8}$")


class RegionClient:
    def __init__(self, index: int, url: str):
        self.index = index
        self.url = url
        self.region = REGIONS[index]
        self.page: Page | None = None
        self.template: dict[str, Any] | None = None
        self.lock = asyncio.Lock()

    async def close(self):
        if self.page:
            await self.page.close()
            self.page = None
        self.template = None

    async def _capture_template(self, page: Page):
        template = None

        async def capture(request):
            nonlocal template
            if template or "querydata" not in request.url.lower():
                return
            try:
                body = json.loads(request.post_data or "{}")
                command = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
                names = [item.get("Name") for item in command["Query"].get("Select", [])]
                if "BASE_HP_F.CEP" in names:
                    template = {"url": request.url, "headers": await request.all_headers(), "body": body}
            except Exception:
                return

        page.on("request", capture)
        for state in BRAZIL_STATES:
            await _select_state(page, state)
            if template:
                break
        page.remove_listener("request", capture)
        if not template:
            raise RuntimeError(f"Não foi possível capturar a consulta do Power BI ({self.region})")
        self.template = template

    async def start(self, browser: Browser):
        if self.page and self.template:
            return
        await self.close()
        self.page = await browser.new_page(viewport={"width": 1280, "height": 900}, locale="pt-BR")
        if not await _login(self.page, self.url):
            raise RuntimeError(f"Falha no login do relatório {self.region}")
        await _clear_report_filters(self.page)
        await self._capture_template(self.page)

    async def query_cep(self, browser: Browser, cep: str) -> list[dict[str, str]]:
        async with self.lock:
            await self.start(browser)
            assert self.page and self.template
            # Reuse the proven extractor query shape, narrowing by exact CEP.
            from extract_nio_facades import request_rows
            rows = await request_rows(self.page, self.template, {"CEP": cep})
            return rows


async def fetch_address(cep: str) -> dict[str, str] | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"https://viacep.com.br/ws/{cep}/json/")
            response.raise_for_status()
            data = response.json()
        if data.get("erro") or not data.get("localidade"):
            return None
        return {
            "cep": data.get("cep", cep),
            "logradouro": data.get("logradouro", ""),
            "bairro": data.get("bairro", ""),
            "municipio": data.get("localidade", ""),
            "uf": data.get("uf", ""),
            "complemento": data.get("complemento", ""),
        }
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=HEADLESS)
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.clients = {region: RegionClient(i, POWERBI_URLS[i]) for i, region in enumerate(REGIONS)}
    yield
    for client in app.state.clients.values():
        await client.close()
    await browser.close()
    await playwright.stop()


app = FastAPI(title="NIO Live Facade Lookup", version="1.0.0", lifespan=lifespan)


def verify_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nio-live-facades"}


@app.get("/api/live-facades")
async def live_facades(
    cep: str = Query(..., min_length=8, max_length=9),
    x_api_key: str | None = Header(None),
):
    verify_key(x_api_key)
    clean = re.sub(r"\D", "", cep)
    if not CEP_RE.fullmatch(clean):
        raise HTTPException(status_code=400, detail="CEP deve ter 8 dígitos")
    address = await fetch_address(clean)
    if not address:
        raise HTTPException(status_code=404, detail="CEP não encontrado no ViaCEP")
    region = STATE_TO_REGION.get(address["uf"])
    if not region:
        raise HTTPException(status_code=422, detail=f"UF sem relatório regional: {address['uf']}")
    client: RegionClient = app.state.clients[region]
    try:
        rows = await client.query_cep(app.state.browser, clean)
    except Exception as exc:
        await client.close()
        raise HTTPException(status_code=502, detail=f"Falha ao consultar Power BI {region}: {exc}") from exc
    return {
        "cep": clean,
        "address": address,
        "region": region,
        "facades": rows,
        "total": len(rows),
        "source": "Power BI NIO",
    }
