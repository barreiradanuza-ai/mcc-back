"""
Sync Nio CEPs from four regional Power BI reports to Postgres.

Each regional report contains a subset of Brazil's CEP coverage. The script
logs into each report, finds the CEP slicer across the report pages, collects
its virtualized list, unions all CEPs, and atomically replaces ceps_nio.
"""

import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

POWERBI_URLS = (
    "https://app.powerbi.com/view?r=eyJrIjoiYTUyMGYwNmUtYzdjZS00OTJmLWIyMTctMjVkNTI0MjM2YTExIiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
    "https://app.powerbi.com/view?r=eyJrIjoiYTMyMWI0MDQtODE4Ni00NjQ1LTgwNzAtNTA3YThmZWE2YWJiIiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
    "https://app.powerbi.com/view?r=eyJrIjoiY2MyMTJjMjUtMWI2YS00MzAxLTg3N2ItNzAzZTJjN2FhNzg4IiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
    "https://app.powerbi.com/view?r=eyJrIjoiODFlOTVjMWEtZTc3MC00NGUzLTk2NDYtMTlkZjg0NDM3NTZjIiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
)
NIO_PASS_1 = "6566"
NIO_PASS_2 = "7791"
DATABASE_URL = os.getenv("DATABASE_URL", "")
HEADLESS = os.getenv("NIO_HEADLESS", "true").lower() != "false"
CEP_PATTERN = re.compile(r"^\d{8}$")
MAX_IDLE_SCROLLS = 15
OUTPUT_CSV = os.getenv("NIO_OUTPUT_CSV", "ceps_nio_novos.csv")
AUDIT_JSON = os.getenv("NIO_AUDIT_JSON", "ceps_nio_auditoria.json")
EXPORT_ONLY = os.getenv("NIO_EXPORT_ONLY", "false").lower() == "true"
TEST_STATES = tuple(s.strip().upper() for s in os.getenv("NIO_TEST_STATES", "").split(",") if s.strip())
ADAPTIVE_STATES = {"PR", "RS"}
ADAPTIVE_LIMIT = 29999
BRAZIL_STATES = tuple("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())


async def _wait_for_report(page):
    for _ in range(45):
        await page.wait_for_timeout(2000)
        body = await page.inner_text("body")
        if "Loading data" not in body and "Carregando dados" not in body:
            try:
                await page.locator("div.slicer-dropdown-menu").first.wait_for(
                    state="visible", timeout=3000
                )
                return
            except Exception:
                pass


async def _select_partner(page):
    user_dropdown = page.locator("div.slicer-dropdown-menu").first
    await user_dropdown.wait_for(state="visible", timeout=60000)
    await user_dropdown.click()
    user_candidates = ("PILOTO", "Todos", "PARCEIRO")
    for user_name in user_candidates:
        candidates = (
            page.get_by_text(user_name, exact=True),
            page.get_by_text(re.compile(rf"^\s*{re.escape(user_name)}\s*$", re.IGNORECASE)),
            page.locator(f"text={user_name}"),
        )
        for partner in candidates:
            if not await partner.count():
                continue
            try:
                await partner.first.wait_for(state="visible", timeout=15000)
                await partner.first.click()
                print(f"[sync] Selected Nio user option: {user_name}")
                return
            except Exception:
                continue
    body = (await page.inner_text("body"))[:500].replace("\n", " | ")
    raise RuntimeError(f"No supported Nio user option found after opening user slicer: {body}")


async def _login(page, report_url: str) -> bool:
    print(f"[sync] Navigating to regional Power BI report: {report_url[-24:]}")
    await page.goto(report_url, wait_until="domcontentloaded", timeout=90000)
    await _wait_for_report(page)

    body = await page.inner_text("body")
    if "LOGIN DE ACESSO" not in body.upper():
        print("[sync] No login screen detected; continuing with public regional report")
        return True

    await _select_partner(page)

    visible_inputs = page.locator("input:visible")
    await visible_inputs.first.wait_for(state="visible", timeout=30000)
    if await visible_inputs.count() < 2:
        print("[sync] Expected 2 password inputs, got fewer")
        return False
    await visible_inputs.nth(0).fill(NIO_PASS_1)
    await visible_inputs.nth(1).fill(NIO_PASS_2)
    await page.get_by_text("ENTRAR", exact=True).first.click()
    await page.wait_for_timeout(15000)
    return True


async def _clear_report_filters(page):
    clear_button = page.get_by_text("Limpar Filtros", exact=True)
    if await clear_button.count() and await clear_button.first.is_visible():
        await clear_button.first.click()
        print("[sync] Cleared default regional report filters")
        await page.wait_for_timeout(10000)


async def _select_state(page, state: str) -> bool:
    """Select one UF in the report so querydata stays below Power BI limits."""
    uf = page.locator("div.slicer-dropdown-menu[aria-label='UF']")
    if not await uf.count() or not await uf.last.is_visible():
        return False
    await uf.last.click()
    await page.wait_for_timeout(1000)
    candidates = page.get_by_text(state, exact=True)
    for i in range(await candidates.count()):
        candidate = candidates.nth(i)
        if await candidate.is_visible():
            await candidate.click()
            await page.wait_for_timeout(8000)
            print(f"[sync] Selected UF {state}")
            return True
    await page.keyboard.press("Escape")
    print(f"[sync] UF {state} not available in this regional report")
    return False


def _value_dicts(payload: dict) -> dict:
    data = payload.get("results", [{}])[0].get("result", {}).get("data", {})
    dsr = data.get("dsr", {})
    dictionaries = {}
    dictionaries.update(dsr.get("ValueDicts", {}) or {})
    for dataset in dsr.get("DS", []) or []:
        dictionaries.update(dataset.get("ValueDicts", {}) or {})
        for phase in dataset.get("PH", []) or []:
            dictionaries.update(phase.get("ValueDicts", {}) or {})
    return dictionaries


def _decode_dsr_cell(value, dictionaries: dict):
    if isinstance(value, dict):
        if "D" in value:
            value = value["D"]
        elif "S" in value:
            value = value["S"]
    if isinstance(value, int):
        # Power BI dictionaries are commonly keyed by the column/group name;
        # support both a direct list and a dict containing a list.
        for dictionary in dictionaries.values():
            if isinstance(dictionary, list) and 0 <= value < len(dictionary):
                return dictionary[value]
        return value
    return value


def _extract_ceps_from_querydata(payload: dict) -> set[str]:
    """Decode CEP values only from DSR responses selecting BASE_HP_F.CEP."""
    data = payload.get("results", [{}])[0].get("result", {}).get("data", {})
    selects = data.get("descriptor", {}).get("Select", []) or []
    cep_indexes = [i for i, item in enumerate(selects) if item.get("Name") == "BASE_HP_F.CEP"]
    if not cep_indexes:
        return set()
    cep_index = cep_indexes[0]
    dictionaries = _value_dicts(payload)
    found: set[str] = set()
    previous: list = []
    dsr = data.get("dsr", {})
    for dataset in dsr.get("DS", []) or []:
        for phase in dataset.get("PH", []) or []:
            for block_name in ("DM0", "DM1"):
                for row in phase.get(block_name, []) or []:
                    direct_value = row.get(f"G{cep_index}")
                    if isinstance(direct_value, str):
                        digits = re.sub(r"\D", "", direct_value)
                        if CEP_PATTERN.fullmatch(digits):
                            found.add(digits)
                        continue
                    cells = row.get("C", [])
                    repeat_mask = row.get("R", 0)
                    decoded = []
                    cell_pos = 0
                    for col in range(len(selects)):
                        if isinstance(repeat_mask, int) and (repeat_mask & (1 << col)):
                            value = previous[col] if col < len(previous) else None
                        else:
                            value = cells[cell_pos] if cell_pos < len(cells) else None
                            cell_pos += 1
                        decoded.append(_decode_dsr_cell(value, dictionaries))
                    previous = decoded
                    value = decoded[cep_index] if cep_index < len(decoded) else None
                    if isinstance(value, str):
                        digits = re.sub(r"\D", "", value)
                        if CEP_PATTERN.fullmatch(digits):
                            found.add(digits)
    return found


async def _collect_querydata_by_state(page, states: tuple[str, ...], audit: list[dict]) -> set[str]:
    """Intercept querydata responses while selecting each UF."""
    captured: set[str] = set()
    response_count = 0
    request_info = None

    async def on_request(request):
        nonlocal request_info
        if request_info or "querydata" not in request.url.lower():
            return
        try:
            body = json.loads(request.post_data or "{}")
            command = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
            names = [item.get("Name") for item in command["Query"].get("Select", [])]
            if "BASE_HP_F.CEP" in names and len(names) > 5:
                request_info = {"url": request.url, "headers": await request.all_headers(), "body": body}
                print(f"[sync] Captured table query template with {len(names)} columns")
        except Exception:
            return

    async def on_response(response):
        nonlocal response_count
        if "querydata" not in response.url.lower():
            return
        try:
            response_count += 1
            try:
                payload = await response.json()
            except Exception:
                return
            values = _extract_ceps_from_querydata(payload)
            if values:
                captured.update(values)
                print(f"[sync] querydata captured +{len(values)} CEPs (total: {len(captured)})")
        except Exception as exc:
            print(f"[sync] querydata response ignored: {exc}")

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        for state in states:
            if await _select_state(page, state):
                await page.wait_for_timeout(5000)
                if request_info:
                    break
        if request_info:
            for state in states:
                if state in ADAPTIVE_STATES:
                    captured.update(await _collect_adaptive_state(page, request_info, state, audit))
                else:
                    values = await _query_cep_only(page, request_info, state)
                    captured.update(values)
                    audit.append({"state": state, "start": None, "end": None,
                                  "count": len(values), "truncated": False,
                                  "depth": None, "status": "state-query"})
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)
    print(f"[sync] querydata responses inspected: {response_count}; CEPs: {len(captured)}")
    return captured


def _make_cep_query(template: dict, state: str, start: int | None = None,
                    end: int | None = None) -> dict:
    """Reduce a captured visual query to CEP only and one UF."""
    body = __import__("copy").deepcopy(template)
    command = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
    query = command["Query"]
    query["Select"] = [{
        "Column": {"Expression": {"SourceRef": {"Source": "b"}}, "Property": "CEP"},
        "Name": "BASE_HP_F.CEP",
        "NativeReferenceName": "CEP",
    }]
    conditions = [{"Condition": {"In": {
        "Expressions": [{"Column": {
            "Expression": {"SourceRef": {"Source": "b"}}, "Property": "UF"
        }}],
        "Values": [[{"Literal": {"Value": f"'{state}'"}}]],
    }}}] if state else []
    if start is not None and end is not None:
        cep_column = {"Column": {"Expression": {"SourceRef": {"Source": "b"}}, "Property": "CEP"}}
        conditions.extend([
            {"Condition": {"Comparison": {"ComparisonKind": 3,
                "Left": cep_column, "Right": {"Literal": {"Value": f"'{start:08d}'"}}}}},
            {"Condition": {"Comparison": {"ComparisonKind": 0,
                "Left": cep_column, "Right": {"Literal": {"Value": f"'{end:08d}'"}}}}},
        ])
    query["Where"] = conditions
    command["Binding"] = {
        "Primary": {"Groupings": [{"Projections": [0], "Subtotal": 1}]},
        "DataReduction": {"DataVolume": 3, "Primary": {"Window": {"Count": 30000}}},
    }
    return body


async def _query_cep_only(page, request_info: dict, state: str,
                          start: int | None = None, end: int | None = None) -> set[str]:
    body = _make_cep_query(request_info["body"], state, start, end)
    collected: set[str] = set()
    page_number = 0
    while page_number < 200:
        result = await page.evaluate("""async ({url, headers, body}) => {
        const safe = {};
        for (const [key, value] of Object.entries(headers)) {
            if (!['content-length', 'host', 'cookie'].includes(key.toLowerCase())) safe[key] = value;
        }
        const response = await fetch(url, {method: 'POST', headers: safe, body: JSON.stringify(body)});
        return {status: response.status, text: await response.text()};
        }""", {"url": request_info["url"], "headers": request_info["headers"], "body": body})
        if result["status"] != 200:
            print(f"[sync] CEP query UF {state} returned HTTP {result['status']}")
            return collected
        try:
            payload = json.loads(result["text"])
            values = _extract_ceps_from_querydata(payload)
        except Exception as exc:
            print(f"[sync] CEP query UF {state} decode failed: {exc}")
            return collected
        collected.update(values)
        dsr = payload.get("results", [{}])[0].get("result", {}).get("data", {}).get("dsr", {})
        phases = [phase for dataset in dsr.get("DS", []) or [] for phase in dataset.get("PH", []) or []]
        restart_tokens = next((phase.get("RT") for phase in phases if phase.get("RT")), None)
        page_number += 1
        label = f"{state or 'regional'} {start:08d}-{end:08d}" if start is not None else (state or "regional")
        print(f"[sync] CEP page {label} #{page_number}: +{len(values)} (total {len(collected)})"
              f"; restart={'yes' if restart_tokens else 'no'}")
        if not restart_tokens or not values:
            return collected
        command = body["queries"][0]["Query"]["Commands"][0]["SemanticQueryDataShapeCommand"]
        command["Binding"]["DataReduction"]["Primary"]["Window"] = {
            "Count": 30000,
            "RestartTokens": restart_tokens,
        }
    print(f"[sync] CEP query {state} stopped after pagination safety limit")
    return collected


def _adaptive_ranges(state: str) -> list[tuple[int, int]]:
    if state == "PR":
        return [(prefix * 1000000, (prefix + 1) * 1000000) for prefix in range(80, 88)]
    if state == "RS":
        return [(prefix * 1000000, (prefix + 1) * 1000000) for prefix in range(90, 100)]
    return []


async def _collect_adaptive_range(page, request_info: dict, state: str,
                                  start: int, end: int, depth: int,
                                  audit: list[dict]) -> set[str]:
    values = await _query_cep_only(page, request_info, state, start, end)
    truncated = len(values) >= ADAPTIVE_LIMIT
    row = {"state": state, "start": f"{start:08d}", "end": f"{end:08d}",
           "count": len(values), "truncated": truncated, "depth": depth,
           "status": "split" if truncated else "complete"}
    audit.append(row)
    if not truncated:
        return values
    if end - start <= 1:
        row["status"] = "failed-minimum-range"
        raise RuntimeError(f"Faixa ainda truncada no menor intervalo: {state} {start}-{end}")
    midpoint = start + (end - start) // 2
    left = await _collect_adaptive_range(page, request_info, state, start, midpoint, depth + 1, audit)
    right = await _collect_adaptive_range(page, request_info, state, midpoint, end, depth + 1, audit)
    return left | right


async def _collect_adaptive_state(page, request_info: dict, state: str,
                                  audit: list[dict]) -> set[str]:
    # CEP comparisons and UF predicates can suppress RestartTokens in this
    # model. Query the regional context without rebuilding Where predicates,
    # paginate with DSR RestartTokens, then partition CEPs locally by range.
    regional = await _query_cep_only(page, request_info, None)
    collected = {cep for cep in regional if _cep_state(cep) == state}
    at_limit = len(collected) == ADAPTIVE_LIMIT
    audit.append({"state": state, "start": None, "end": None,
                  "count": len(collected), "truncated": at_limit,
                  "depth": 0,
                  "status": "limit-without-restart-token" if at_limit
                  else "restart-token-paginated",
                  "complete_confirmed": not at_limit})
    print(f"[sync] Restart-token extraction UF {state}: {len(collected)} CEPs")
    return collected


async def _open_cep_slicer(page):
    cep_dropdown = page.locator("div.slicer-dropdown-menu[aria-label='CEP']")
    for page_number in range(4):
        if await cep_dropdown.count() and await cep_dropdown.first.is_visible():
            return cep_dropdown
        next_page = page.get_by_role("button", name="Próxima Página")
        if page_number == 3 or not await next_page.count():
            break
        await next_page.click()
        await page.wait_for_timeout(5000)
    await cep_dropdown.wait_for(state="visible", timeout=30000)
    return cep_dropdown


async def _prepare_table(page):
    slider = page.locator('[role="slider"][aria-label="TopN_Parametro"]')
    if await slider.count() and await slider.first.is_visible():
        await slider.first.focus()
        for _ in range(1000):
            value = await slider.first.get_attribute("aria-valuenow")
            if value and int(value) >= 1000:
                break
            await page.keyboard.press("ArrowRight")
        print(f"[sync] Table row parameter: {await slider.first.get_attribute('aria-valuenow')}")
        await page.wait_for_timeout(15000)


async def _collect_from_table(page) -> set[str]:
    await _prepare_table(page)
    collected: set[str] = set()
    grid_cells = page.locator("[role='gridcell']:visible")
    for i in range(await grid_cells.count()):
        text = (await grid_cells.nth(i).inner_text()).strip()
        collected.update(re.findall(r"\b\d{8}\b", text))
    print(f"[sync] Table fallback collected {len(collected)} unique CEPs")
    return {cep for cep in collected if CEP_PATTERN.match(cep)}


async def _collect_all_ceps(page, audit: list[dict]) -> set[str]:
    """Open the CEP slicer and scroll through collecting all visible CEPs."""
    await _clear_report_filters(page)
    cep_dropdown = await _open_cep_slicer(page)
    states = TEST_STATES or BRAZIL_STATES
    querydata_ceps = await _collect_querydata_by_state(page, states, audit)
    if querydata_ceps:
        print(f"[sync] Regional querydata extraction collected {len(querydata_ceps)} unique CEPs")
        return querydata_ceps
    await cep_dropdown.click()
    await page.wait_for_timeout(5000)

    search_input = page.locator("input[placeholder='Search']:visible")
    if await search_input.count() == 0:
        search_input = page.locator("input.searchInput:visible")
    await search_input.first.wait_for(state="visible", timeout=5000)

    input_box = await search_input.first.bounding_box()
    wheel_x = (input_box["x"] + input_box["width"] / 2) if input_box else 640
    wheel_y = (input_box["y"] + input_box["height"] + 80) if input_box else 500
    await page.mouse.move(wheel_x, wheel_y)
    await page.mouse.wheel(0, 200)
    await page.wait_for_timeout(1000)

    collected: set[str] = set()
    idle_count = 0
    while idle_count < MAX_IDLE_SCROLLS:
        value_locators = (
            page.locator(".slicerText:visible"),
            page.locator("[role='option']:visible"),
        )
        before = len(collected)
        for values in value_locators:
            count = await values.count()
            for i in range(count):
                text = (await values.nth(i).inner_text()).strip()
                for candidate in re.findall(r"\b\d{8}\b", text):
                    if CEP_PATTERN.match(candidate):
                        collected.add(candidate)
        new_items = len(collected) - before
        if new_items > 0:
            idle_count = 0
            print(f"[sync] +{new_items} CEPs (total: {len(collected)})")
        else:
            idle_count += 1
        await page.mouse.move(wheel_x, wheel_y)
        await page.mouse.wheel(0, 300)
        await page.wait_for_timeout(600)

    if not collected:
        collected = await _collect_from_table(page)
    print(f"[sync] Regional report done. Collected {len(collected)} unique CEPs")
    return collected


async def scrape_nio_ceps(audit: list[dict]) -> set[str]:
    """Collect and union CEPs from all four regional reports."""
    all_ceps: set[str] = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900}, locale="pt-BR"
        )
        try:
            for report_url in POWERBI_URLS:
                try:
                    if await _login(page, report_url):
                        all_ceps.update(await _collect_all_ceps(page, audit))
                except Exception as exc:
                    print(f"[sync] Regional report failed: {exc}")
                await page.goto("about:blank")
        finally:
            await browser.close()
    print(f"[sync] All regional reports done. Collected {len(all_ceps)} unique CEPs")
    return all_ceps


def save_to_db(ceps: set[str]):
    """Truncate ceps_nio and insert all collected CEPs. Update nio_cache_meta."""
    if not DATABASE_URL:
        print("Error: DATABASE_URL environment variable is not set.")
        sys.exit(1)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ceps_nio (cep CHAR(8) PRIMARY KEY);
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS nio_cache_meta (
                    id INT PRIMARY KEY CHECK (id = 1),
                    updated_at TIMESTAMPTZ,
                    total INT
                );
            """)
            cur.execute("TRUNCATE TABLE ceps_nio")
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO ceps_nio (cep) VALUES %s ON CONFLICT DO NOTHING",
                [(c,) for c in ceps],
                page_size=5000,
            )
            now = datetime.now(timezone.utc)
            cur.execute("""
                INSERT INTO nio_cache_meta (id, updated_at, total)
                VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET updated_at = %s, total = %s
            """, (now, len(ceps), now, len(ceps)))
        conn.commit()
        print(f"[sync] Saved {len(ceps)} CEPs to ceps_nio. Meta updated.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def write_ceps_csv(ceps: set[str], path: str = OUTPUT_CSV) -> None:
    """Write only the validated, unique CEP column for manual import."""
    normalized = sorted({cep for cep in ceps if CEP_PATTERN.fullmatch(cep)})
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["CEP"])
        writer.writerows((cep,) for cep in normalized)
    print(f"[sync] Wrote {len(normalized)} validated unique CEPs to {path}")


def write_audit(audit: list[dict], path: str = AUDIT_JSON) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(audit, handle, ensure_ascii=False, indent=2)
    print(f"[sync] Wrote audit report with {len(audit)} queries to {path}")


def write_state_csv(ceps: set[str], state: str) -> None:
    path = f"ceps_{state.lower()}.csv"
    write_ceps_csv({cep for cep in ceps if _cep_state(cep) == state}, path)


def _cep_state(cep: str) -> str | None:
    n = int(cep)
    if 80000000 <= n < 88000000:
        return "PR"
    if 90000000 <= n < 100000000:
        return "RS"
    return None


def main():
    print(f"[sync] Starting Nio CEP sync at {datetime.now(timezone.utc).isoformat()}")
    audit: list[dict] = []
    ceps = asyncio.run(scrape_nio_ceps(audit))
    write_ceps_csv(ceps)
    write_audit(audit)
    write_state_csv(ceps, "PR")
    write_state_csv(ceps, "RS")
    if not ceps:
        print("[sync] No CEPs collected — aborting DB write to avoid wiping table.")
        sys.exit(1)
    if EXPORT_ONLY:
        print("[sync] Export-only mode enabled; database was not modified.")
        return
    save_to_db(ceps)
    print(f"[sync] Done. {len(ceps)} CEPs synced.")


if __name__ == "__main__":
    main()
