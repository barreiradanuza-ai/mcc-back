"""
Nio coverage checker via Power BI embedded report.
Uses Playwright to automate login and CEP lookup in the slicer dropdown.
"""

import asyncio
import os
import re
from playwright.async_api import async_playwright

POWERBI_URLS = (
    "https://app.powerbi.com/view?r=eyJrIjoiYTUyMGYwNmUtYzdjZS00OTJmLWIyMTctMjVkNTI0MjM2YTExIiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
    "https://app.powerbi.com/view?r=eyJrIjoiYTMyMWI0MDQtODE4Ni00NjQ1LTgwNzAtNTA3YThmZWE2YWJiIiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
    "https://app.powerbi.com/view?r=eyJrIjoiY2MyMTJjMjUtMWI2YS00MzAxLTg3N2ItNzAzZTJjN2FhNzg4IiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
    "https://app.powerbi.com/view?r=eyJrIjoiODFlOTVjMWEtZTc3MC00NGUzLTk2NDYtMTlkZjg0NDM3NTZjIiwidCI6Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9",
)

NIO_PASS_1 = "6566"
NIO_PASS_2 = "7791"

HEADLESS = os.getenv("NIO_HEADLESS", "true").lower() != "false"


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
                print(f"[nio] Selected Nio user option: {user_name}")
                return
            except Exception:
                continue
    body = (await page.inner_text("body"))[:500].replace("\n", " | ")
    raise RuntimeError(f"No supported Nio user option found after opening user slicer: {body}")


async def _check_nio_report(page, cep_clean: str, report_url: str) -> bool:
    await page.goto(report_url, wait_until="networkidle", timeout=90000)

    for _ in range(15):
        await page.wait_for_timeout(2000)
        body = await page.inner_text("body")
        if "Loading data" not in body and "Carregando dados" not in body:
            break

    await _select_partner(page)

    visible_inputs = page.locator("input:visible")
    await visible_inputs.first.wait_for(state="visible", timeout=30000)
    if await visible_inputs.count() < 2:
        return False
    await visible_inputs.nth(0).fill(NIO_PASS_1)
    await visible_inputs.nth(1).fill(NIO_PASS_2)
    await page.get_by_text("ENTRAR", exact=True).first.click()
    await page.wait_for_timeout(10000)

    cep_dropdown = page.locator("div.slicer-dropdown-menu[aria-label='CEP']")
    for page_number in range(4):
        if await cep_dropdown.count() and await cep_dropdown.first.is_visible():
            break
        next_page = page.get_by_role("button", name="Próxima Página")
        if page_number == 3 or not await next_page.count():
            await cep_dropdown.wait_for(state="visible", timeout=30000)
            break
        await next_page.click()
        await page.wait_for_timeout(5000)

    await cep_dropdown.click()
    search_input = page.locator("input[placeholder='Search']:visible")
    if await search_input.count() == 0:
        search_input = page.locator("input.searchInput:visible")
    await search_input.first.wait_for(state="visible", timeout=5000)
    await search_input.first.fill("")
    await search_input.first.type(cep_clean, delay=80)

    for _ in range(16):
        await page.wait_for_timeout(500)
        if await page.locator(".slicerText:visible").count() > 0:
            break
        if await page.get_by_text("Nenhum resultado encontrado").count() > 0:
            return False

    input_box = await search_input.first.bounding_box()
    wheel_x = (input_box["x"] + input_box["width"] / 2) if input_box else 640
    wheel_y = (input_box["y"] + input_box["height"] + 80) if input_box else 500
    for attempt in range(6):
        slicer_texts = page.locator(".slicerText:visible")
        for i in range(await slicer_texts.count()):
            if (await slicer_texts.nth(i).inner_text()).strip() == cep_clean:
                return True
        if await page.get_by_text("Nenhum resultado encontrado").count() > 0:
            return False
        if attempt < 5:
            await page.mouse.move(wheel_x, wheel_y)
            await page.mouse.wheel(0, 200)
            await page.wait_for_timeout(1200)
    return False


async def _check_nio_async(cep: str) -> bool:
    cep_clean = re.sub(r"\D", "", cep)
    if len(cep_clean) != 8:
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900},
            locale="pt-BR",
        )

        try:
            for report_url in POWERBI_URLS:
                try:
                    if await _check_nio_report(page, cep_clean, report_url):
                        await browser.close()
                        return True
                except Exception as exc:
                    print(f"[Nio] report failed: {exc}")
                await page.goto("about:blank")

            await browser.close()
            return False

        except Exception as exc:
            print(f"[Nio] unexpected error: {exc}")
            await browser.close()
            return False


MAX_NIO_ATTEMPTS = 2


def _run_async(coro):
    """Run an async coroutine from sync context, handling existing event loops."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=120)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def check_nio_coverage(cep: str) -> bool:
    """Synchronous wrapper with retry for the async Nio coverage check."""
    for attempt in range(MAX_NIO_ATTEMPTS):
        result = _run_async(_check_nio_async(cep))
        if result:
            return True
        if attempt < MAX_NIO_ATTEMPTS - 1:
            print(f"[Nio] attempt {attempt + 1} returned False, retrying in 3s...")
            import time
            time.sleep(3)
    return False
