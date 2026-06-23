"""
Sync Nio CEPs from Power BI to Postgres.

Opens one Playwright session, logs in, opens the CEP slicer, and scrolls
through the virtualized list collecting every visible CEP. Then truncates
and repopulates the ceps_nio table.

Usage:
    DATABASE_URL=postgres://... python sync_nio_ceps.py
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

POWERBI_URL = (
    "https://app.powerbi.com/view?r="
    "eyJrIjoiOGE5ZGI4ZjktN2NmMS00ZGI1LTkwZDItNTI1OWFkMTQ5ZWJhIiwidCI6"
    "Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9"
    "&disablecdnExpiration=1769028883"
)

NIO_PASS_1 = "6566"
NIO_PASS_2 = "7791"
DATABASE_URL = os.getenv("DATABASE_URL", "")
HEADLESS = os.getenv("NIO_HEADLESS", "true").lower() != "false"
CEP_PATTERN = re.compile(r"^\d{8}$")
MAX_IDLE_SCROLLS = 15


async def _login(page) -> bool:
    """Navigate to Power BI, wait for load, and log in. Returns True on success."""
    print("[sync] Navigating to Power BI...")
    await page.goto(POWERBI_URL, wait_until="networkidle", timeout=60000)

    for _ in range(10):
        await page.wait_for_timeout(2000)
        body = await page.inner_text("body")
        if "Loading data" not in body:
            break

    user_dropdown = page.locator("div.slicer-dropdown-menu").first
    await user_dropdown.click()

    parceiro = page.get_by_text("PARCEIRO", exact=True)
    try:
        await parceiro.first.wait_for(state="visible", timeout=5000)
    except Exception:
        print("[sync] PARCEIRO not found")
        return False
    await parceiro.first.click()

    visible_inputs = page.locator("input:visible")
    try:
        await visible_inputs.first.wait_for(state="visible", timeout=5000)
    except Exception:
        print("[sync] Password inputs not found")
        return False

    if await visible_inputs.count() < 2:
        print("[sync] Expected 2 password inputs, got fewer")
        return False

    await visible_inputs.nth(0).click()
    await visible_inputs.nth(0).fill(NIO_PASS_1)
    await visible_inputs.nth(1).click()
    await visible_inputs.nth(1).fill(NIO_PASS_2)

    entrar = page.get_by_text("ENTRAR", exact=True)
    try:
        await entrar.first.wait_for(state="visible", timeout=3000)
    except Exception:
        print("[sync] ENTRAR button not found")
        return False
    await entrar.first.click()

    cep_dropdown = page.locator("div.slicer-dropdown-menu[aria-label='CEP']")
    try:
        await cep_dropdown.wait_for(state="visible", timeout=15000)
    except Exception:
        print("[sync] CEP slicer not visible after login")
        return False

    print("[sync] Login successful")
    return True


async def _collect_all_ceps(page) -> set[str]:
    """Open the CEP slicer and scroll through collecting all CEPs."""
    cep_dropdown = page.locator("div.slicer-dropdown-menu[aria-label='CEP']")
    await cep_dropdown.click()
    await page.wait_for_timeout(1500)

    search_input = page.locator("input[placeholder='Search']:visible")
    try:
        await search_input.first.wait_for(state="visible", timeout=5000)
    except Exception:
        search_input = page.locator("input.searchInput:visible")
        await search_input.first.wait_for(state="visible", timeout=3000)

    input_box = await search_input.first.bounding_box()
    wheel_x = (input_box["x"] + input_box["width"] / 2) if input_box else 640
    wheel_y = (input_box["y"] + input_box["height"] + 80) if input_box else 500

    # Trigger initial render
    await page.mouse.move(wheel_x, wheel_y)
    await page.mouse.wheel(0, 200)
    await page.wait_for_timeout(1000)

    collected: set[str] = set()
    idle_count = 0

    while idle_count < MAX_IDLE_SCROLLS:
        slicer_texts = page.locator(".slicerText:visible")
        count = await slicer_texts.count()
        before = len(collected)

        for i in range(count):
            text = (await slicer_texts.nth(i).inner_text()).strip()
            if CEP_PATTERN.match(text):
                collected.add(text)

        new_items = len(collected) - before
        if new_items > 0:
            idle_count = 0
            print(f"[sync] +{new_items} CEPs (total: {len(collected)})")
        else:
            idle_count += 1

        await page.mouse.move(wheel_x, wheel_y)
        await page.mouse.wheel(0, 300)
        await page.wait_for_timeout(600)

    print(f"[sync] Scrolling done. Collected {len(collected)} unique CEPs")
    return collected


async def scrape_nio_ceps() -> set[str]:
    """Full pipeline: open browser, login, collect CEPs."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900},
            locale="pt-BR",
        )
        try:
            if not await _login(page):
                await browser.close()
                return set()
            ceps = await _collect_all_ceps(page)
            await browser.close()
            return ceps
        except Exception as exc:
            print(f"[sync] Unexpected error: {exc}")
            await browser.close()
            return set()


def save_to_db(ceps: set[str]):
    """Truncate ceps_nio and insert all collected CEPs. Update nio_cache_meta."""
    if not DATABASE_URL:
        print("Error: DATABASE_URL environment variable is not set.")
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ceps_nio (
                    cep CHAR(8) PRIMARY KEY
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS nio_cache_meta (
                    id INT PRIMARY KEY CHECK (id = 1),
                    updated_at TIMESTAMPTZ,
                    total INT
                );
            """)
            cur.execute("TRUNCATE TABLE ceps_nio")

            if ceps:
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
    except Exception as e:
        conn.rollback()
        print(f"[sync] DB error: {e}")
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
