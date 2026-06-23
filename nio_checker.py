"""
Nio coverage checker via Power BI embedded report.
Uses Playwright to automate login and CEP lookup in the slicer dropdown.
"""

import asyncio
import os
import re
from playwright.async_api import async_playwright

POWERBI_URL = (
    "https://app.powerbi.com/view?r="
    "eyJrIjoiOGE5ZGI4ZjktN2NmMS00ZGI1LTkwZDItNTI1OWFkMTQ5ZWJhIiwidCI6"
    "Ijg1YjI4NDIxLWQ0NWEtNGIwNy04ODlkLTI0YjUyOGM3ZjI1MCJ9"
    "&disablecdnExpiration=1769028883"
)

NIO_PASS_1 = "6566"
NIO_PASS_2 = "7791"

HEADLESS = os.getenv("NIO_HEADLESS", "true").lower() != "false"


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
            await page.goto(POWERBI_URL, wait_until="networkidle", timeout=90000)

            for _ in range(15):
                await page.wait_for_timeout(2000)
                body = await page.inner_text("body")
                if "Loading data" not in body:
                    break

            # Step 1: Select PARCEIRO from the user dropdown (first slicer)
            user_dropdown = page.locator("div.slicer-dropdown-menu").first
            await user_dropdown.click()

            parceiro = page.get_by_text("PARCEIRO", exact=True)
            try:
                await parceiro.first.wait_for(state="visible", timeout=5000)
            except Exception:
                await browser.close()
                return False
            await parceiro.first.click()

            # Step 2: Fill Senha 1 and Senha 2 — wait for inputs to appear first
            visible_inputs = page.locator("input:visible")
            try:
                await visible_inputs.first.wait_for(state="visible", timeout=5000)
            except Exception:
                await browser.close()
                return False

            if await visible_inputs.count() < 2:
                await browser.close()
                return False

            await visible_inputs.nth(0).click()
            await visible_inputs.nth(0).fill(NIO_PASS_1)
            await visible_inputs.nth(1).click()
            await visible_inputs.nth(1).fill(NIO_PASS_2)

            # Step 3: Click ENTRAR — wait for the button to be enabled/visible first
            entrar = page.get_by_text("ENTRAR", exact=True)
            try:
                await entrar.first.wait_for(state="visible", timeout=3000)
            except Exception:
                await browser.close()
                return False
            await entrar.first.click()
            # Wait until the CEP slicer is visible — dashboard is ready
            cep_dropdown = page.locator("div.slicer-dropdown-menu[aria-label='CEP']")
            try:
                await cep_dropdown.wait_for(state="visible", timeout=15000)
            except Exception:
                await browser.close()
                return False

            # Step 4: Open the CEP slicer dropdown
            if await cep_dropdown.count() == 0:
                await browser.close()
                return False
            await cep_dropdown.click()

            # Step 5: Type CEP in the search field — wait for it to appear
            search_input = page.locator("input[placeholder='Search']:visible")
            try:
                await search_input.first.wait_for(state="visible", timeout=5000)
            except Exception:
                search_input = page.locator("input.searchInput:visible")
                try:
                    await search_input.first.wait_for(state="visible", timeout=3000)
                except Exception:
                    await browser.close()
                    return False

            # Click to focus, then type char-by-char to trigger Power BI's live filter
            await search_input.first.click()
            await search_input.first.fill("")
            await page.wait_for_timeout(200)
            await search_input.first.type(cep_clean, delay=80)

            # Wait for the slicer filter to stabilize instead of a fixed delay.
            # Power BI's async filter may still be showing stale items right after
            # typing, so we wait until the visible count is stable across 2 reads.
            prev_count = -1
            for _ in range(16):
                await page.wait_for_timeout(500)
                cur_count = await page.locator(".slicerText:visible").count()
                no_results = await page.get_by_text("Nenhum resultado encontrado").count() > 0
                if no_results:
                    break
                if cur_count == prev_count:
                    break
                prev_count = cur_count

            # Step 6: Check the filtered list.
            # PowerBI's slicer uses virtualized rendering — the DOM only populates
            # items after a real wheel event is received. We do up to 5 wheel
            # triggers to coax the list into rendering, then stop.
            input_box = await search_input.first.bounding_box()
            wheel_x = (input_box["x"] + input_box["width"] / 2) if input_box else 640
            wheel_y = (input_box["y"] + input_box["height"] + 80) if input_box else 500

            found = False
            items_seen_once = False
            for attempt in range(6):
                slicer_texts = page.locator(".slicerText:visible")
                count = await slicer_texts.count()

                for i in range(count):
                    text = (await slicer_texts.nth(i).inner_text()).strip()
                    if text == cep_clean:
                        found = True
                        break

                print(
                    f"[Nio] attempt {attempt}: {count} item(s) visible, found={found}"
                )

                if found:
                    break

                if await page.get_by_text("Nenhum resultado encontrado").count() > 0:
                    print("[Nio] 'Nenhum resultado encontrado' — CEP not in coverage")
                    break

                # Items appeared but CEP not among them: wait and re-check once
                # to handle stale/residual items from before the filter completed.
                if count > 0:
                    if items_seen_once:
                        break
                    items_seen_once = True
                    await page.wait_for_timeout(1500)
                    continue

                # Nothing rendered yet: send a wheel event to trigger virtual list render
                if attempt < 5:
                    await page.mouse.move(wheel_x, wheel_y)
                    await page.mouse.wheel(0, 200)
                    await page.wait_for_timeout(1200)

            await browser.close()
            return found

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
