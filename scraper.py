"""
Yandex DataLens scraper for https://datalens.yandex/6dr39r9a9l9mt

Extracts player → decks mapping by:
1. Opening the dashboard in a headless browser (Playwright)
2. Intercepting API responses from the DataLens charts service
3. Iterating through each player option in the selector widget
4. Collecting deck data returned for each player

Usage:
    python scraper.py                   # scrape all players, save to output.json + output.csv
    python scraper.py --player "Name"   # scrape a single player
    python scraper.py --debug           # show browser window + save screenshots
"""

import asyncio
import argparse
import json
import csv
import re
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page, BrowserContext, Response

DASHBOARD_URL = "https://datalens.yandex/6dr39r9a9l9mt"

# DataLens / Gravity UI CSS selectors (based on open-source datalens-ui + @gravity-ui/uikit)
SEL_SELECT_CONTROL = (
    "[class*='select__control'],"
    "[class*='Select-control'],"
    "[class*='dl-selector'],"
    "button[class*='select']"
)
SEL_SELECT_OPTION = (
    "[class*='select-list__option'],"
    "[class*='Select-option'],"
    "[class*='g-select-list__option'],"
    "[role='option']"
)
SEL_TABLE_CELL = (
    "td,"
    "[class*='chartkit-table'] td,"
    "[class*='Table__cell']"
)


# ─── API interception helpers ────────────────────────────────────────────────

def is_chart_response(url: str) -> bool:
    """Return True if the URL looks like a DataLens chart-data endpoint."""
    patterns = [
        r"/api/run",
        r"charts\.yandex",
        r"datalens\.yandex.*/(run|data|chart)",
        r"/api/embeds/",
    ]
    return any(re.search(p, url) for p in patterns)


def extract_rows_from_body(body: Any) -> list[dict]:
    """
    Attempt to pull tabular rows out of a DataLens chart API response.
    DataLens returns data in a variety of shapes; we try the common ones.
    """
    rows: list[dict] = []

    if not isinstance(body, dict):
        return rows

    # Shape 1: {"data": {"rows": [[val, ...]], "columns": [{"name": ...}]}}
    data = body.get("data", {})
    if isinstance(data, dict):
        columns = data.get("columns", [])
        col_names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in columns]
        for row in data.get("rows", []):
            if col_names:
                rows.append(dict(zip(col_names, row)))
            else:
                rows.append({"value": row})

    # Shape 2: {"result": {"data": {"Data": [[...]], "Type": [...]}}}
    result = body.get("result", {})
    if isinstance(result, dict):
        inner = result.get("data", {})
        if isinstance(inner, dict):
            data_rows = inner.get("Data", [])
            types = inner.get("Type", [])
            for row in data_rows:
                rows.append(dict(enumerate(row)))

    # Shape 3: flat list at top level
    if not rows and isinstance(body, list):
        rows = body

    return rows


# ─── Core scraper ────────────────────────────────────────────────────────────

class DataLensScraper:
    def __init__(self, headless: bool = True, debug: bool = False):
        self.headless = headless
        self.debug = debug
        self._captured: list[dict] = []   # raw API payloads captured during a player selection

    async def _on_response(self, response: Response) -> None:
        if not is_chart_response(response.url):
            return
        try:
            body = await response.json()
            self._captured.append({"url": response.url, "body": body})
            if self.debug:
                print(f"  [API] {response.url}")
        except Exception:
            pass  # binary / non-JSON responses

    async def _get_player_options(self, page: Page) -> list[str]:
        """
        Find the selector widget, open it, and return all player option labels.
        Tries several strategies in order.
        """
        # Strategy A: native <select> element
        selects = await page.query_selector_all("select")
        for sel in selects:
            options = await sel.query_selector_all("option")
            values = [await o.inner_text() for o in options]
            values = [v.strip() for v in values if v.strip()]
            if values:
                print(f"  Found native <select> with {len(values)} options.")
                return values

        # Strategy B: @gravity-ui/uikit Select (renders a <button>)
        control = await page.query_selector(SEL_SELECT_CONTROL)
        if control:
            await control.click()
            await page.wait_for_timeout(800)

            option_els = await page.query_selector_all(SEL_SELECT_OPTION)
            if option_els:
                names = [await el.inner_text() for el in option_els]
                names = [n.strip() for n in names if n.strip()]
                print(f"  Found Gravity-UI Select with {len(names)} options.")
                # Close the popup before returning
                await page.keyboard.press("Escape")
                return names

        # Strategy C: any element with role="listbox" or role="combobox"
        combos = await page.query_selector_all("[role='combobox'], [role='listbox']")
        for combo in combos:
            await combo.click()
            await page.wait_for_timeout(600)
            option_els = await page.query_selector_all("[role='option']")
            if option_els:
                names = [await el.inner_text() for el in option_els]
                names = [n.strip() for n in names if n.strip()]
                print(f"  Found combobox/listbox with {len(names)} options.")
                await page.keyboard.press("Escape")
                return names

        return []

    async def _select_player(self, page: Page, player: str) -> bool:
        """Select a specific player in the selector widget. Returns True on success."""

        # Try native <select>
        selects = await page.query_selector_all("select")
        for sel in selects:
            options = await sel.query_selector_all("option")
            for opt in options:
                text = (await opt.inner_text()).strip()
                if text == player:
                    await sel.select_option(label=player)
                    return True

        # Try Gravity-UI Select button → click option by text
        control = await page.query_selector(SEL_SELECT_CONTROL)
        if control:
            await control.click()
            await page.wait_for_timeout(600)

            option_els = await page.query_selector_all(SEL_SELECT_OPTION)
            for el in option_els:
                text = (await el.inner_text()).strip()
                if text == player:
                    await el.click()
                    return True
            # No match found; close popup
            await page.keyboard.press("Escape")

        # Try role=option
        await page.keyboard.press("Escape")
        combos = await page.query_selector_all("[role='combobox'], [role='listbox']")
        for combo in combos:
            await combo.click()
            await page.wait_for_timeout(600)
            option_els = await page.query_selector_all("[role='option']")
            for el in option_els:
                text = (await el.inner_text()).strip()
                if text == player:
                    await el.click()
                    return True
            await page.keyboard.press("Escape")

        return False

    async def _read_table_from_dom(self, page: Page) -> list[dict]:
        """Fallback: read visible table rows from the DOM."""
        rows: list[dict] = []
        tables = await page.query_selector_all("table")
        for table in tables:
            headers_els = await table.query_selector_all("th")
            headers = [((await h.inner_text()).strip()) for h in headers_els]

            body_rows = await table.query_selector_all("tbody tr")
            for tr in body_rows:
                cells = await tr.query_selector_all("td")
                values = [(await c.inner_text()).strip() for c in cells]
                if not any(values):
                    continue
                if headers and len(headers) == len(values):
                    rows.append(dict(zip(headers, values)))
                else:
                    rows.append({str(i): v for i, v in enumerate(values)})
        return rows

    async def scrape(self, target_player: str | None = None) -> dict[str, list[dict]]:
        """
        Main entry point. Returns {player_name: [deck_row, ...], ...}.
        """
        results: dict[str, list[dict]] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="ru-RU",
            )
            page = await context.new_page()
            page.on("response", self._on_response)

            print(f"Opening {DASHBOARD_URL} …")
            await page.goto(DASHBOARD_URL, wait_until="networkidle", timeout=60_000)

            if self.debug:
                await page.screenshot(path="debug_initial.png")
                print("  Screenshot saved: debug_initial.png")

            # Extra wait for lazy-loaded JS widgets
            await page.wait_for_timeout(3_000)

            players = await self._get_player_options(page)

            if not players:
                print("  WARNING: Could not find any player options in the selector.")
                print("  Dumping page text for manual inspection …")
                text = await page.inner_text("body")
                Path("debug_page_text.txt").write_text(text, encoding="utf-8")
                await browser.close()
                return results

            if target_player:
                players = [p for p in players if p == target_player]
                if not players:
                    print(f"  Player '{target_player}' not found. Available: {players}")
                    await browser.close()
                    return results

            print(f"Found {len(players)} player(s): {players}\n")

            for player in players:
                print(f"Selecting player: {player}")
                self._captured.clear()

                ok = await self._select_player(page, player)
                if not ok:
                    print(f"  Could not select '{player}', skipping.")
                    continue

                # Wait for charts to reload
                await page.wait_for_load_state("networkidle", timeout=15_000)
                await page.wait_for_timeout(1_500)

                if self.debug:
                    safe = player.replace(" ", "_").replace("/", "-")
                    await page.screenshot(path=f"debug_{safe}.png")

                # 1) Try to get data from intercepted API responses
                deck_rows: list[dict] = []
                for capture in self._captured:
                    rows = extract_rows_from_body(capture["body"])
                    if rows:
                        deck_rows.extend(rows)
                        if self.debug:
                            print(f"  Got {len(rows)} rows from {capture['url']}")

                # 2) Fallback: read table DOM
                if not deck_rows:
                    deck_rows = await self._read_table_from_dom(page)
                    if deck_rows:
                        print(f"  Got {len(deck_rows)} rows from DOM table.")

                results[player] = deck_rows
                print(f"  → {len(deck_rows)} deck row(s) collected.\n")

            await browser.close()

        return results


# ─── Output helpers ──────────────────────────────────────────────────────────

def save_json(results: dict[str, list[dict]], path: str = "output.json") -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Saved JSON → {path}")


def save_csv(results: dict[str, list[dict]], path: str = "output.csv") -> None:
    """Flatten player→decks into a single CSV with a 'player' column."""
    all_rows: list[dict] = []
    for player, decks in results.items():
        for deck in decks:
            all_rows.append({"player": player, **deck})

    if not all_rows:
        print("No rows to write to CSV.")
        return

    fieldnames = list(all_rows[0].keys())
    # Ensure 'player' is first column
    if "player" in fieldnames:
        fieldnames = ["player"] + [k for k in fieldnames if k != "player"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Saved CSV  → {path}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Yandex DataLens player/deck data")
    parser.add_argument("--player", metavar="NAME", help="Scrape only this player (default: all)")
    parser.add_argument("--out-json", default="output.json", metavar="FILE", help="JSON output path")
    parser.add_argument("--out-csv", default="output.csv", metavar="FILE", help="CSV output path")
    parser.add_argument("--debug", action="store_true", help="Show browser + save screenshots")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    scraper = DataLensScraper(headless=not args.headed, debug=args.debug)

    results = await scraper.scrape(target_player=args.player)

    if not results:
        print("No data collected.")
        sys.exit(1)

    save_json(results, args.out_json)
    save_csv(results, args.out_csv)

    total_decks = sum(len(v) for v in results.values())
    print(f"\nDone. {len(results)} player(s), {total_decks} total deck row(s).")


if __name__ == "__main__":
    asyncio.run(main())
