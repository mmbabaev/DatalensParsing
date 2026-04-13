"""
DataLens API discoverer.

Given a public DataLens dashboard URL, fetches the page HTML + JS bundles
and tries to extract:
  - chart/widget keys embedded in the dashboard config
  - API endpoint patterns used by the frontend
  - any embedded JSON state (window.__initialState__, __DATA__, etc.)

Then attempts direct API calls to those endpoints.

Usage:
    python discover.py
    python discover.py --url https://datalens.yandex/XXXX
"""

import re
import json
import argparse
import textwrap
from urllib.parse import urljoin, urlparse

import requests

DASHBOARD_URL = "https://datalens.yandex/6dr39r9a9l9mt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ─── helpers ─────────────────────────────────────────────────────────────────

def get(url: str, **kw) -> requests.Response | None:
    try:
        r = SESSION.get(url, timeout=15, **kw)
        print(f"  GET {url}  →  {r.status_code}")
        return r
    except Exception as e:
        print(f"  GET {url}  →  ERROR: {e}")
        return None


def post(url: str, **kw) -> requests.Response | None:
    try:
        r = SESSION.post(url, timeout=15, **kw)
        print(f"  POST {url}  →  {r.status_code}")
        return r
    except Exception as e:
        print(f"  POST {url}  →  ERROR: {e}")
        return None


def find_json_blobs(text: str) -> list[dict | list]:
    """Find all JSON objects/arrays embedded in a string."""
    blobs = []
    for m in re.finditer(r'(\{[\s\S]{20,}?\}|\[[\s\S]{20,}?\])', text):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            pass
    return blobs


def extract_script_urls(html: str, base_url: str) -> list[str]:
    return [
        urljoin(base_url, src)
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    ]


def find_api_patterns(js: str) -> list[str]:
    """Search JS source for API URL patterns."""
    hits = set()
    for m in re.finditer(r'["\`](\/?api\/[^\s"\'`\)]{3,})["\`]', js):
        hits.add(m.group(1))
    for m in re.finditer(r'["\`](\/charts\/[^\s"\'`\)]{3,})["\`]', js):
        hits.add(m.group(1))
    return sorted(hits)


def find_chart_keys(js_or_html: str) -> list[str]:
    """
    DataLens chart/widget keys look like short alphanumeric slugs,
    often 8-24 chars. They appear next to keywords like "key", "chartId",
    "entryId", "widgetId", "id".
    """
    hits = set()
    patterns = [
        r'"(?:key|chartId|entryId|widgetId|id)"\s*:\s*"([a-z0-9]{8,32})"',
        r"'(?:key|chartId|entryId|widgetId|id)'\s*:\s*'([a-z0-9]{8,32})'",
        r'/run/([a-z0-9_-]{8,})',
        r'/embeds/([a-z0-9_-]{8,})',
    ]
    for pat in patterns:
        for m in re.finditer(pat, js_or_html, re.IGNORECASE):
            hits.add(m.group(1))
    return sorted(hits)


def extract_window_state(html: str) -> dict:
    """
    DataLens (and many React SPAs) embed initial state as:
      window.__DATA__ = {...}
      window.__initialState__ = {...}
      window.DATALENS_STORE = {...}
    """
    state = {}
    patterns = [
        r'window\.__DATA__\s*=\s*(\{[\s\S]*?\});',
        r'window\.__initialState__\s*=\s*(\{[\s\S]*?\});',
        r'window\.DATALENS_STORE\s*=\s*(\{[\s\S]*?\});',
        r'window\.__APP_STATE__\s*=\s*(\{[\s\S]*?\});',
        r'<script[^>]*>\s*window\[[\"\']DL[\"\'\]]+\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>',
        r'__INITIAL_STATE__\s*=\s*(\{[\s\S]*?\});',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            try:
                state[pat[:40]] = json.loads(m.group(1))
                print(f"  Found window state: {pat[:60]}")
            except Exception:
                state[pat[:40]] = m.group(1)[:200]
    return state


def try_known_api_endpoints(base: str, dash_id: str) -> None:
    """
    Try a set of known DataLens / Yandex API URL patterns.
    """
    origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    candidates = [
        f"{origin}/api/v1/dash/{dash_id}",
        f"{origin}/api/v2/dash/{dash_id}",
        f"{origin}/api/dash/{dash_id}",
        f"{origin}/api/v1/entry/{dash_id}",
        f"{origin}/api/v1/config/{dash_id}",
        f"{origin}/api/entries/{dash_id}",
        f"{origin}/api/public/v1/dash/{dash_id}",
        f"{origin}/api/run/{dash_id}",
        # DataLens US (UnitedStorage) typical paths
        f"{origin}/gateway/us/v1/entry/{dash_id}",
        f"{origin}/gateway/us/v2/entry/{dash_id}",
    ]
    print("\n── Trying known API patterns ──────────────────────────────")
    for url in candidates:
        r = get(url)
        if r and r.status_code == 200:
            print(f"  ✓ HIT: {url}")
            try:
                print(textwrap.indent(json.dumps(r.json(), ensure_ascii=False)[:500], "    "))
            except Exception:
                print(f"    (non-JSON response, {len(r.content)} bytes)")


def try_run_chart(origin: str, chart_key: str, params: dict | None = None) -> None:
    """Try to fetch chart data via /api/run/{key}."""
    url = f"{origin}/api/run/{chart_key}"
    body = {"params": params or {}}
    print(f"\n── Trying /api/run/{chart_key}")
    r = post(url, json=body)
    if r and r.status_code == 200:
        print(f"  ✓ HIT!")
        try:
            print(textwrap.indent(json.dumps(r.json(), ensure_ascii=False)[:600], "  "))
        except Exception:
            print(f"  (non-JSON, {len(r.content)} bytes)")


# ─── main ─────────────────────────────────────────────────────────────────────

def main(dashboard_url: str) -> None:
    parsed = urlparse(dashboard_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    dash_id = parsed.path.strip("/")

    print(f"\n{'='*60}")
    print(f"  Analyzing: {dashboard_url}")
    print(f"  Origin:    {origin}")
    print(f"  Dash ID:   {dash_id}")
    print(f"{'='*60}\n")

    # ── Step 1: fetch the dashboard HTML ──────────────────────────────────────
    print("── Step 1: Fetch HTML ─────────────────────────────────────")
    r = get(dashboard_url)
    if r is None:
        print("FATAL: cannot reach the server.")
        return

    html = r.text
    print(f"  HTML size: {len(html):,} chars")

    # ── Step 2: look for embedded window state ────────────────────────────────
    print("\n── Step 2: Embedded window state ──────────────────────────")
    window_state = extract_window_state(html)
    if window_state:
        with open("window_state.json", "w", encoding="utf-8") as f:
            json.dump(window_state, f, ensure_ascii=False, indent=2)
        print("  Saved → window_state.json")
    else:
        print("  None found.")

    # ── Step 3: find chart keys in HTML ──────────────────────────────────────
    print("\n── Step 3: Chart keys in HTML ─────────────────────────────")
    html_keys = find_chart_keys(html)
    print(f"  Found {len(html_keys)} key candidates: {html_keys[:10]}")

    # ── Step 4: find & fetch JS bundles ──────────────────────────────────────
    print("\n── Step 4: JS bundles ─────────────────────────────────────")
    script_urls = extract_script_urls(html, dashboard_url)
    print(f"  Found {len(script_urls)} <script src=...> tags")

    all_js = ""
    for url in script_urls[:8]:   # limit to first 8 bundles
        jr = get(url)
        if jr and jr.status_code == 200:
            all_js += jr.text

    print(f"  Total JS downloaded: {len(all_js):,} chars")

    # ── Step 5: API patterns in JS ────────────────────────────────────────────
    print("\n── Step 5: API patterns in JS ─────────────────────────────")
    api_patterns = find_api_patterns(all_js)
    if api_patterns:
        print(f"  Found {len(api_patterns)} API patterns:")
        for p in api_patterns[:30]:
            print(f"    {p}")
    else:
        print("  None found.")

    # ── Step 6: chart keys in JS ──────────────────────────────────────────────
    print("\n── Step 6: Chart keys in JS ───────────────────────────────")
    js_keys = find_chart_keys(all_js)
    all_keys = sorted(set(html_keys + js_keys))
    print(f"  Found {len(js_keys)} key candidates in JS.")
    if all_keys:
        print(f"  All unique keys: {all_keys[:20]}")

    # ── Step 7: try known REST endpoints ─────────────────────────────────────
    try_known_api_endpoints(dashboard_url, dash_id)

    # ── Step 8: try /api/run for discovered chart keys ────────────────────────
    if all_keys:
        print("\n── Step 8: Try /api/run for chart keys ────────────────────")
        for key in all_keys[:5]:
            try_run_chart(origin, key)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"  API patterns found in JS: {len(api_patterns)}")
    print(f"  Chart key candidates:     {len(all_keys)}")
    print(f"  Window state captured:    {bool(window_state)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DASHBOARD_URL)
    args = parser.parse_args()
    main(args.url)
