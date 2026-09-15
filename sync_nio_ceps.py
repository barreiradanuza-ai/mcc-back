"""
Sync Nio CEPs from four regional Power BI reports to Postgres.

Each regional report contains a subset of Brazil's CEP coverage. The script
logs into each report, finds the CEP slicer across the report pages, collects
its virtualized list, unions all CEPs, and atomically replaces ceps_nio.
"""

import asyncio
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
    await page.goto(report_url, wait_until="networkidle", timeout=90000)
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
    await page.wait_for_timeout(10000)
    return True


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


async def _collect_all_ceps(page) -> set[str]:
    """Open the CEP slicer and scroll through collecting all visible CEPs."""
    cep_dropdown = await _open_cep_slicer(page)
    await cep_dropdown.click()
    await page.wait_for_timeout(1500)

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

    print(f"[sync] Regional report done. Collected {len(collected)} unique CEPs")
    return collected


async def scrape_nio_ceps() -> set[str]:
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
                        all_ceps.update(await _collect_all_ceps(page))
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


def main():
    print(f"[sync] Starting Nio CEP sync at {datetime.now(timezone.utc).isoformat()}")
    ceps = asyncio.run(scrape_nio_ceps())
    if not ceps:
        print("[sync] No CEPs collected — aborting DB write to avoid wiping table.")
        sys.exit(1)
    save_to_db(ceps)
    print(f"[sync] Done. {len(ceps)} CEPs synced.")


if __name__ == "__main__":
    main()


