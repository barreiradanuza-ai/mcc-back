"""Extract complete viable-facade rows from the five NIO regional reports.

This job is intentionally separate from the CEP-only exporter. It writes a
staging CSV and audit JSON; database replacement must happen only after all
regions and UFs pass validation.
"""
import asyncio, csv, json, os, re
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright
from sync_nio_ceps import (
    POWERBI_URLS, HEADLESS, BRAZIL_STATES, _login, _wait_for_report,
    _clear_report_filters, _select_state, _query_column, _value_dicts,
    _decode_dsr_cell,
)

FIELDS = ("UF", "MUNICIPIO", "BAIRRO", "LOGRADOURO", "NO_FACHADA",
          "COMPLEMENTO1", "COMPLEMENTO2", "COMPLEMENTO3", "CEP",
          "VIABILIDADE_ATUAL", "CODIGO_LOGRADOURO", "CODIGO_CDO",
          "CLASSIFICACAO", "CELULA", "ESTACAO")
OUTPUT = Path(os.getenv("NIO_FACADES_CSV", "super_lista_nio_staging.csv"))
AUDIT = Path(os.getenv("NIO_FACADES_AUDIT", "super_lista_nio_auditoria.json"))


def make_rows_query(template, filters):
    import copy
    body = copy.deepcopy(template)
    command = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    source = "b"
    command["Query"]["Select"] = [
        {"Column": {"Expression": {"SourceRef": {"Source": source}}, "Property": f},
         "Name": f"BASE_HP_F.{f}", "NativeReferenceName": f}
        for f in FIELDS
    ]
    command["Query"]["Where"] = [
        {"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": source}}, "Property": k}}],
        "Values": [[{"Literal": {"Value": "'" + str(v).replace("'", "''") + "'"}}]]}}}
        for k, v in filters.items()
    ]
    command["Binding"] = {"Primary": {"Groupings": [{"Projections": list(range(len(FIELDS))), "Subtotal": 1}]},
                           "DataReduction": {"DataVolume": 6, "Primary": {"Window": {"Count": 30000}}}}
    return body


def decode_rows(payload):
    data = payload.get("results", [{}])[0].get("result", {}).get("data", {})
    selects = data.get("descriptor", {}).get("Select", []) or []
    names = [x.get("Name", "").split(".")[-1] for x in selects]
    if not names: return []
    dictionaries = _value_dicts(payload)
    out, previous = [], []
    dsr = data.get("dsr", {})
    for dataset in dsr.get("DS", []) or []:
        for phase in dataset.get("PH", []) or []:
            for block in ("DM0", "DM1"):
                for row in phase.get(block, []) or []:
                    cells, mask, decoded, pos = row.get("C", []), row.get("R", 0), [], 0
                    for col in range(len(names)):
                        if isinstance(mask, int) and mask & (1 << col): value = previous[col] if col < len(previous) else None
                        else:
                            value = cells[pos] if pos < len(cells) else None; pos += 1
                            value = _decode_dsr_cell(value, dictionaries)
                        decoded.append(value)
                    previous = decoded
                    record = {names[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(decoded)}
                    if record.get("CEP") and re.fullmatch(r"\d{8}", re.sub(r"\D", "", record["CEP"])):
                        record["CEP"] = re.sub(r"\D", "", record["CEP"])
                        out.append(record)
    return out


async def request_rows(page, info, filters):
    body = make_rows_query(info["body"], filters)
    result = await page.evaluate("""async ({url, headers, body}) => {
      const safe={}; for (const [k,v] of Object.entries(headers)) if (!['content-length','host','cookie'].includes(k.toLowerCase())) safe[k]=v;
      const r=await fetch(url,{method:'POST',headers:safe,body:JSON.stringify(body)}); return {status:r.status,text:await r.text()};
    }""", {"url": info["url"], "headers": info["headers"], "body": body})
    if result["status"] != 200: return []
    return decode_rows(json.loads(result["text"]))


async def scrape():
    rows, audit = {}, []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(viewport={"width": 1280, "height": 900}, locale="pt-BR")
        try:
            for index, url in enumerate(POWERBI_URLS):
                region = ["Centro Oeste", "Norte e Nordeste", "Sudeste", "Sul", "Sao Paulo"][index]
                if not await _login(page, url): raise RuntimeError(f"Login falhou: {region}")
                await _clear_report_filters(page)
                info = None
                async def capture(request):
                    nonlocal info
                    if info or "querydata" not in request.url.lower(): return
                    try:
                        body=json.loads(request.post_data or "{}"); cmd=body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
                        if any(x.get("Name") == "BASE_HP_F.CEP" for x in cmd["Query"].get("Select", [])):
                            info={"url":request.url,"headers":await request.all_headers(),"body":body}
                    except Exception: pass
                page.on("request", capture)
                for state in BRAZIL_STATES:
                    await _select_state(page, state)
                    if info: break
                page.remove_listener("request", capture)
                if not info: raise RuntimeError(f"Modelo de consulta não capturado: {region}")
                available = 0
                for state in BRAZIL_STATES:
                    if not await _select_state(page, state): continue
                    municipalities = await _query_column(page, info, "MUNICIPIO", {"UF": state})
                    for municipality in sorted(municipalities):
                        part = await request_rows(page, info, {"UF": state, "MUNICIPIO": municipality})
                        # Keep only rows explicitly marked viable when the report returns a value.
                        for record in part:
                            viability = record.get("VIABILIDADE_ATUAL", "").upper()
                            if not viability or any(x in viability for x in ("VIAVEL", "VIÁVEL", "OK", "SIM")):
                                key = tuple(record.get(f, "") for f in FIELDS)
                                rows[key] = {**record, "REGIAO": region, "RELATORIO_ORIGEM": url}
                        available += len(part)
                    audit.append({"region": region, "state": state, "municipios": len(municipalities), "rows": available})
                await page.goto("about:blank")
        finally: await browser.close()
    ordered = list(rows.values())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        writer=csv.DictWriter(f, fieldnames=list(FIELDS)+["REGIAO","RELATORIO_ORIGEM"]); writer.writeheader(); writer.writerows(ordered)
    AUDIT.write_text(json.dumps({"finished_at":datetime.now(timezone.utc).isoformat(),"rows":len(ordered),"queries":audit},ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"[facades] wrote {len(ordered)} rows to {OUTPUT}")

if __name__ == "__main__": asyncio.run(scrape())
