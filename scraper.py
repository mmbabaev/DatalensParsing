"""
Yandex DataLens scraper for https://datalens.yandex/6dr39r9a9l9mt

Extracts player → decks mapping by:
1. Opening the dashboard in a headless browser (Playwright)
2. Intercepting API responses from the DataLens charts service
3. Iterating through each player option in the selector widget
4. Collecting deck data returned for each player

Usage:
    python scraper.py                      # scrape all players → output.json + output.csv
    python scraper.py --player "Name"      # scrape a single player
    python scraper.py --inspect-network    # dump ALL network requests to network_dump.json
    python scraper.py --debug              # verbose + save page text
"""

import asyncio
import argparse
import json
import csv
import re
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page, BrowserContext, Response, Request

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


# ─── Network capture helpers ─────────────────────────────────────────────────

def is_chart_api(url: str) -> bool:
    """Return True if the URL looks like a DataLens chart-data endpoint."""
    return bool(re.search(
        r"(/api/run|/api/embeds|charts\.yandex|datalens\.yandex.*(run|data|chart))",
        url,
    ))


def extract_rows_from_body(body: Any) -> list[dict]:
    """
    Pull tabular rows out of a DataLens chart API response.
    DataLens returns data in several shapes; we try each.
    """
    rows: list[dict] = []
    if not isinstance(body, dict):
        return rows

    # Shape 1: {"data": {"rows": [[...]], "columns": [{"name": ...}, ...]}}
    data = body.get("data", {})
    if isinstance(data, dict):
        columns = data.get("columns", [])
        col_names = [c.get("name", str(i)) if isinstance(c, dict) else str(c)
                     for i, c in enumerate(columns)]
        for row in data.get("rows", []):
            rows.append(dict(zip(col_names, row)) if col_names else {"value": row})

    # Shape 2: {"result": {"data": {"Data": [[...]], ...}}}
    result = body.get("result", {})
    if isinstance(result, dict):
        inner = result.get("data", {})
        if isinstance(inner, dict):
            for row in inner.get("Data", []):
                rows.append(dict(enumerate(row)))

    # Shape 3: flat list at top level
    if not rows and isinstance(body, list):
        rows = list(body)

    return rows


# ─── Core scraper ────────────────────────────────────────────────────────────

class DataLensScraper:
    def __init__(self, headless: bool = True, debug: bool = False,
                 inspect_network: bool = False):
        self.headless = headless
        self.debug = debug
        self.inspect_network = inspect_network

        self._chart_responses: list[dict] = []   # chart API captures (current player)
        self._all_traffic: list[dict] = []        # full traffic dump (--inspect-network)

    # ── response listener ────────────────────────────────────────────────────

    async def _on_response(self, response: Response) -> None:
        url = response.url
        status = response.status
        content_type = response.headers.get("content-type", "")

        if "json" not in content_type:
            if self.inspect_network:
                self._all_traffic.append({
                    "url": url,
                    "status": status,
                    "content_type": content_type,
                    "body": None,
                })
            return

        try:
            body = await response.json()
        except Exception:
            body = None

        if self.inspect_network:
            self._all_traffic.append({
                "url": url,
                "status": status,
                "content_type": content_type,
                "body": body,
            })

        if is_chart_api(url) and body is not None:
            self._chart_responses.append({"url": url, "body": body})
            if self.debug:
                print(f"  [chart API] {url}")

    # ── find player options ──────────────────────────────────────────────────

    async def _get_player_options(self, page: Page) -> list[str]:
        """Find the selector widget and return all option labels."""

        # Strategy A: native <select>
        for sel in await page.query_selector_all("select"):
            options = await sel.query_selector_all("option")
            values = [(await o.inner_text()).strip() for o in options]
            values = [v for v in values if v]
            if values:
                print(f"  Selector: native <select>, {len(values)} options.")
                return values

        # Strategy B: @gravity-ui/uikit Select (rendered as <button>)
        control = await page.query_selector(SEL_SELECT_CONTROL)
        if control:
            await control.click()
            await page.wait_for_timeout(800)
            option_els = await page.query_selector_all(SEL_SELECT_OPTION)
            if option_els:
                names = [(await el.inner_text()).strip() for el in option_els]
                names = [n for n in names if n]
                print(f"  Selector: Gravity-UI Select, {len(names)} options.")
                await page.keyboard.press("Escape")
                return names

        # Strategy C: role=combobox / listbox
        for combo in await page.query_selector_all("[role='combobox'],[role='listbox']"):
            await combo.click()
            await page.wait_for_timeout(600)
            option_els = await page.query_selector_all("[role='option']")
            if option_els:
                names = [(await el.inner_text()).strip() for el in option_els]
                names = [n for n in names if n]
                print(f"  Selector: ARIA combobox/listbox, {len(names)} options.")
                await page.keyboard.press("Escape")
                return names

        return []

    # ── select a player ──────────────────────────────────────────────────────

    async def _select_player(self, page: Page, player: str) -> bool:
        # Native <select>
        for sel in await page.query_selector_all("select"):
            for opt in await sel.query_selector_all("option"):
                if (await opt.inner_text()).strip() == player:
                    await sel.select_option(label=player)
                    return True

        # Gravity-UI Select
        control = await page.query_selector(SEL_SELECT_CONTROL)
        if control:
            await control.click()
            await page.wait_for_timeout(600)
            for el in await page.query_selector_all(SEL_SELECT_OPTION):
                if (await el.inner_text()).strip() == player:
                    await el.click()
                    return True
            await page.keyboard.press("Escape")

        # ARIA combobox / listbox
        await page.keyboard.press("Escape")
        for combo in await page.query_selector_all("[role='combobox'],[role='listbox']"):
            await combo.click()
            await page.wait_for_timeout(600)
            for el in await page.query_selector_all("[role='option']"):
                if (await el.inner_text()).strip() == player:
                    await el.click()
                    return True
            await page.keyboard.press("Escape")

        return False

    # ── DOM table fallback ───────────────────────────────────────────────────

    async def _read_tables_from_dom(self, page: Page) -> list[dict]:
        rows: list[dict] = []
        for table in await page.query_selector_all("table"):
            headers = [(await h.inner_text()).strip()
                       for h in await table.query_selector_all("th")]
            for tr in await table.query_selector_all("tbody tr"):
                values = [(await c.inner_text()).strip()
                          for c in await tr.query_selector_all("td")]
                if not any(values):
                    continue
                if headers and len(headers) == len(values):
                    rows.append(dict(zip(headers, values)))
                else:
                    rows.append({str(i): v for i, v in enumerate(values)})
        return rows

    # ── screenshot (best-effort) ─────────────────────────────────────────────

    async def _screenshot(self, page: Page, path: str) -> None:
        try:
            await page.screenshot(path=path, timeout=10_000)
            print(f"  Screenshot → {path}")
        except Exception as e:
            print(f"  Screenshot skipped ({e})")

    # ── main scrape ──────────────────────────────────────────────────────────

    async def scrape(self, target_player: str | None = None) -> dict[str, list[dict]]:
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
            try:
                # Use 'domcontentloaded' – doesn't wait for external fonts/CDN
                await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                print(f"  goto() raised: {e} – continuing anyway")

            # Wait for JS widgets to render
            await page.wait_for_timeout(5_000)

            if self.debug:
                await self._screenshot(page, "debug_initial.png")
                text = await page.inner_text("body")
                Path("debug_page_text.txt").write_text(text, encoding="utf-8")
                print("  Page text → debug_page_text.txt")

            # ── inspect-network mode ─────────────────────────────────────────
            if self.inspect_network:
                # Wait a bit more to catch lazy-loaded requests
                await page.wait_for_timeout(5_000)
                dump_path = "network_dump.json"
                with open(dump_path, "w", encoding="utf-8") as f:
                    json.dump(self._all_traffic, f, ensure_ascii=False, indent=2)
                print(f"\n  Captured {len(self._all_traffic)} responses → {dump_path}")
                json_only = [r for r in self._all_traffic if r["body"] is not None]
                print(f"  Of which {len(json_only)} are JSON.\n")
                print("  Top URLs (JSON responses):")
                for r in json_only[:30]:
                    print(f"    [{r['status']}] {r['url']}")
                await browser.close()
                return results

            # ── normal scrape mode ───────────────────────────────────────────
            players = await self._get_player_options(page)

            if not players:
                print("\n  WARNING: no selector options found.")
                print("  Re-run with --inspect-network to see all API calls,")
                print("  or with --debug to dump page text.")
                await browser.close()
                return results

            if target_player:
                players = [p for p in players if p == target_player]
                if not players:
                    print(f"  Player '{target_player}' not found.")
                    await browser.close()
                    return results

            print(f"\nFound {len(players)} player(s): {players}\n")

            for player in players:
                print(f"Selecting: {player}")
                self._chart_responses.clear()

                ok = await self._select_player(page, player)
                if not ok:
                    print(f"  Could not select '{player}', skipping.\n")
                    continue

                # Wait for chart widgets to refresh
                try:
                    await page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    await page.wait_for_timeout(3_000)

                if self.debug:
                    safe = re.sub(r"[^\w]", "_", player)
                    await self._screenshot(page, f"debug_{safe}.png")

                # 1) API-intercepted data
                deck_rows: list[dict] = []
                for capture in self._chart_responses:
                    rows = extract_rows_from_body(capture["body"])
                    if rows:
                        deck_rows.extend(rows)
                        if self.debug:
                            print(f"  {len(rows)} rows from {capture['url']}")

                # 2) DOM fallback
                if not deck_rows:
                    deck_rows = await self._read_tables_from_dom(page)
                    if deck_rows:
                        print(f"  {len(deck_rows)} rows from DOM table (fallback).")

                results[player] = deck_rows
                print(f"  → {len(deck_rows)} deck row(s)\n")

            await browser.close()

        return results


# ─── Output ──────────────────────────────────────────────────────────────────

def save_json(results: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Saved JSON → {path}")


def save_csv(results: dict, path: str) -> None:
    all_rows: list[dict] = []
    for player, decks in results.items():
        for deck in decks:
            all_rows.append({"player": player, **deck})

    if not all_rows:
        print("No rows to write to CSV.")
        return

    fieldnames = ["player"] + [k for k in all_rows[0] if k != "player"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Saved CSV  → {path}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Yandex DataLens player/deck data")
    parser.add_argument("--player", metavar="NAME", help="Scrape only this player")
    parser.add_argument("--out-json", default="output.json", metavar="FILE")
    parser.add_argument("--out-csv",  default="output.csv",  metavar="FILE")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose output + save page text + screenshots")
    parser.add_argument("--headed", action="store_true",
                        help="Run with visible browser window")
    parser.add_argument("--inspect-network", action="store_true",
                        help="Dump all network JSON responses to network_dump.json, then exit")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    scraper = DataLensScraper(
        headless=not args.headed,
        debug=args.debug,
        inspect_network=args.inspect_network,
    )

    results = await scraper.scrape(target_player=args.player)

    if args.inspect_network:
        return  # already printed summary

    if not results:
        print("No data collected.")
        sys.exit(1)

    save_json(results, args.out_json)
    save_csv(results, args.out_csv)

    total = sum(len(v) for v in results.values())
    print(f"\nDone: {len(results)} player(s), {total} total deck row(s).")


if __name__ == "__main__":
    asyncio.run(main())
