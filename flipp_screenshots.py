#!/usr/bin/env python3
"""
Flipp Campaign Screenshot Tool
================================
Captures full-page screenshots of the Flipp browse screen for campaign placement
verification — desktop and mobile viewports — across Vancouver, Toronto, and Montreal.

For each city + viewport, the script reloads the browse screen up to MAX_ATTEMPTS
times until a rendered GAM ad slot is detected (non-zero bounding box), then saves
the screenshot. If no ad is detected after all attempts, it saves a "no_ad" screenshot
so you still have a record.

Usage:
    python flipp_screenshots.py --brands "Coca-Cola, Pepsi, Lays"
    python flipp_screenshots.py --brands "Nike" --output ./screenshots --slack-channel "#campaign-screenshots"
    python flipp_screenshots.py --brands "Nike" --attempts 25   # more reload attempts

Requirements:
    pip install playwright slack-sdk
    playwright install chromium
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    print("❌ Missing dependency: playwright\n   Run: pip install playwright && playwright install chromium")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

CITIES = {
    "Vancouver": "V6B1A1",
    "Toronto":   "M5V2T6",
    "Montreal":  "H3A1A1",
}

VIEWPORTS = {
    "desktop": {
        "width": 1440, "height": 900,
        "is_mobile": False, "device_scale_factor": 1,
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    },
    "mobile": {
        "width": 390, "height": 844,
        "is_mobile": True, "device_scale_factor": 3,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    },
}

BROWSE_URL = "https://flipp.com/en-ca/flyers"

# GAM slot selectors — same DOM structure on both viewports
GAM_SELECTORS = [
    '[id^="div-gpt-ad"]',               # slot container
    'iframe[id^="google_ads_iframe"]',   # rendered ad iframe inside slot
]

# GAM ad request domains to intercept for brand targeting params
GAM_REQUEST_DOMAINS = [
    "securepubads.g.doubleclick.net",
    "pubads.g.doubleclick.net",
    "googleads.g.doubleclick.net",
]

# Custom targeting keys Flipp may use — we check all of them
BRAND_TARGETING_KEYS = ["brand", "advertiser", "client", "campaign", "sponsor"]

# How long to wait after each reload before checking for ads (ms)
RELOAD_SETTLE_MS = 4000


# ── Location setter ───────────────────────────────────────────────────────────

async def set_location(page: Page, postal_code: str) -> None:
    """Try multiple strategies to set the postal code on the browse screen."""
    await page.wait_for_timeout(RELOAD_SETTLE_MS)

    input_selectors = [
        'input[placeholder*="postal" i]',
        'input[placeholder*="code" i]',
        'input[placeholder*="location" i]',
        'input[aria-label*="postal" i]',
        'input[aria-label*="location" i]',
        '[data-testid*="postal"]',
        '[data-testid*="location"] input',
    ]

    # Strategy 1 — visible input already on screen
    for sel in input_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1200):
                await el.fill(postal_code)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2500)
                return
        except Exception:
            continue

    # Strategy 2 — click a location button, then fill the input that appears
    for btn_sel in [
        'button:has-text("location")', 'button:has-text("postal")',
        '[data-testid*="location"]', '[aria-label*="location" i]',
        'text="Set your location"', 'text="Change location"',
    ]:
        try:
            btn = page.locator(btn_sel).first
            if await btn.is_visible(timeout=1200):
                await btn.click()
                await page.wait_for_timeout(1200)
                for inp_sel in input_selectors:
                    try:
                        inp = page.locator(inp_sel).first
                        if await inp.is_visible(timeout=1200):
                            await inp.fill(postal_code)
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(2500)
                            return
                    except Exception:
                        continue
        except Exception:
            continue

    # Strategy 3 — URL param fallback
    await page.goto(
        f"{BROWSE_URL}?postal_code={postal_code}",
        wait_until="networkidle", timeout=20000
    )
    await page.wait_for_timeout(2500)


async def dismiss_overlays(page: Page) -> None:
    for sel in ['button:has-text("Close")', 'button:has-text("✕")', '[aria-label="Close"]']:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=600):
                await btn.click()
                await page.wait_for_timeout(400)
        except Exception:
            pass


# ── GAM ad detection + brand extraction ──────────────────────────────────────

async def gam_ad_is_rendered(page: Page) -> bool:
    """Return True if at least one GAM slot has a non-zero bounding box."""
    for sel in GAM_SELECTORS:
        try:
            elements = page.locator(sel)
            count = await elements.count()
            for i in range(count):
                box = await elements.nth(i).bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    return True
        except Exception:
            continue
    return False


def _parse_cust_params(url: str) -> dict[str, str]:
    """Extract key=value pairs from the cust_params query string in a GAM request URL."""
    from urllib.parse import urlparse, parse_qs, unquote
    try:
        qs = parse_qs(urlparse(url).query)
        raw = qs.get("cust_params", [""])[0]
        pairs = {}
        for part in unquote(raw).split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k.strip()] = v.strip()
        return pairs
    except Exception:
        return {}


async def detect_brand(page: Page, known_brands: list[str]) -> str | None:
    """
    Try to identify which brand's ad is showing using two strategies:

    1. Inspect captured GAM network request URLs for brand targeting params
       (cust_params keys like brand=, advertiser=, etc.)
    2. Query window.googletag slot targeting as a fallback.

    If a known brand (from --brands) is found, return its name.
    Otherwise return whatever targeting value was found, or None.
    """
    brand_lower = {b.lower(): b for b in known_brands}

    # ── Strategy 1: check captured GAM request URLs ───────────────────────────
    captured_params: list[dict] = await page.evaluate("""
        () => window.__flipp_gam_params__ || []
    """)

    for params in captured_params:
        for key in BRAND_TARGETING_KEYS:
            val = params.get(key, "").strip()
            if not val:
                continue
            # Match against known brands (case-insensitive substring)
            for bl, original in brand_lower.items():
                if bl in val.lower() or val.lower() in bl:
                    return original
            # No match but we have a value — return it as-is
            return val

    # ── Strategy 2: read window.googletag slot targeting ─────────────────────
    try:
        slots_data: list[dict] = await page.evaluate("""
            () => {
                if (!window.googletag || !window.googletag.pubads) return [];
                return window.googletag.pubads().getSlots().map(slot => {
                    const targeting = {};
                    // getTargetingKeys() lists all custom keys set on this slot
                    (slot.getTargetingKeys ? slot.getTargetingKeys() : []).forEach(k => {
                        targeting[k] = (slot.getTargeting(k) || []).join(',');
                    });
                    return { unit: slot.getAdUnitPath(), targeting };
                });
            }
        """)
        for slot in slots_data:
            t = slot.get("targeting", {})
            for key in BRAND_TARGETING_KEYS:
                val = t.get(key, "").strip()
                if not val:
                    continue
                for bl, original in brand_lower.items():
                    if bl in val.lower() or val.lower() in bl:
                        return original
                return val
    except Exception:
        pass

    return None


async def install_gam_request_listener(page: Page) -> None:
    """
    Inject a script that intercepts GAM fetch/XHR requests and stores
    their parsed cust_params in window.__flipp_gam_params__.
    Must be called before page.goto().
    """
    await page.add_init_script(f"""
        window.__flipp_gam_params__ = [];
        const GAM_DOMAINS = {GAM_REQUEST_DOMAINS};

        const origFetch = window.fetch;
        window.fetch = function(input, init) {{
            const url = typeof input === 'string' ? input : (input.url || '');
            if (GAM_DOMAINS.some(d => url.includes(d))) {{
                try {{
                    const u = new URL(url);
                    const raw = u.searchParams.get('cust_params') || '';
                    const pairs = {{}};
                    decodeURIComponent(raw).split('&').forEach(p => {{
                        const [k, v] = p.split('=');
                        if (k) pairs[k] = v || '';
                    }});
                    window.__flipp_gam_params__.push(pairs);
                }} catch(e) {{}}
            }}
            return origFetch.apply(this, arguments);
        }};

        // Also cover XMLHttpRequest (older GPT versions)
        const origOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {{
            if (typeof url === 'string' && GAM_DOMAINS.some(d => url.includes(d))) {{
                try {{
                    const u = new URL(url);
                    const raw = u.searchParams.get('cust_params') || '';
                    const pairs = {{}};
                    decodeURIComponent(raw).split('&').forEach(p => {{
                        const [k, v] = p.split('=');
                        if (k) pairs[k] = v || '';
                    }});
                    window.__flipp_gam_params__.push(pairs);
                }} catch(e) {{}}
            }}
            return origOpen.apply(this, arguments);
        }};
    """)


# ── Core capture loop ─────────────────────────────────────────────────────────

async def capture_until_ad(
    context: BrowserContext,
    city: str,
    postal_code: str,
    viewport_name: str,
    output_dir: Path,
    date_str: str,
    max_attempts: int,
    known_brands: list[str],
) -> tuple[Path | None, bool, str | None]:
    """
    Reload the browse screen up to max_attempts times until a rendered GAM ad
    is detected. Returns (screenshot_path, ad_found, detected_brand).
    """
    page = await context.new_page()

    # Install the GAM request interceptor before the first navigation
    await install_gam_request_listener(page)

    # Set location once — persists via cookies/localStorage for subsequent reloads
    try:
        print(f"    → Loading browse screen & setting location ({postal_code})…")
        await page.goto(BROWSE_URL, wait_until="networkidle", timeout=30000)
        await set_location(page, postal_code)
        await dismiss_overlays(page)
    except Exception as e:
        print(f"    ✗ Failed to load page: {e}")
        await page.close()
        return None, False, None

    ad_found      = False
    detected_brand: str | None = None

    for attempt in range(1, max_attempts + 1):
        print(f"    → Attempt {attempt}/{max_attempts} — checking for GAM ad…", end=" ", flush=True)

        if await gam_ad_is_rendered(page):
            detected_brand = await detect_brand(page, known_brands)
            brand_label = f" ({detected_brand})" if detected_brand else ""
            print(f"✓ Ad detected!{brand_label}")
            ad_found = True
            break
        else:
            print("no ad yet, reloading…")
            try:
                await page.reload(wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(RELOAD_SETTLE_MS)
                await dismiss_overlays(page)
            except Exception:
                await page.wait_for_timeout(2000)

    # Build filename — include brand slug if we identified one
    brand_slug = f"_{detected_brand.lower().replace(' ', '-')}" if detected_brand else ""
    suffix     = f"ad_found{brand_slug}" if ad_found else "no_ad"
    filename   = f"{date_str}_{city.replace(' ', '_')}_{viewport_name}_{suffix}.png"
    out_path   = output_dir / filename

    try:
        await page.evaluate("window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })")
        await page.wait_for_timeout(1500)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(800)
        await page.screenshot(path=str(out_path), full_page=True)
        print(f"    {'✅' if ad_found else '⚠️ '} Saved: {out_path.name}")
    except Exception as e:
        print(f"    ✗ Screenshot failed: {e}")
        out_path = None

    await page.close()
    return out_path, ad_found, detected_brand


# ── Slack upload ──────────────────────────────────────────────────────────────

def upload_to_slack(
    screenshots: list[Path],
    channel: str,
    brands: list[str],
    date_str: str,
    token: str,
) -> None:
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        print("  ⚠  slack-sdk not installed — skipping Slack upload. Run: pip install slack-sdk")
        return

    client = WebClient(token=token)
    brand_text = ", ".join(brands) if brands else "all placements"

    # Group by city
    by_city: dict[str, list[Path]] = {}
    for p in screenshots:
        # e.g. 2026-06-19_Vancouver_desktop_ad_found.png → city = Vancouver
        parts = p.stem.split("_")
        city = parts[1] if len(parts) >= 2 else "Unknown"
        by_city.setdefault(city, []).append(p)

    for city, paths in by_city.items():
        file_uploads = []
        for path in paths:
            viewport = "Desktop" if "desktop" in path.stem else "Mobile"
            if "no_ad" in path.stem:
                status = "⚠️ No ad detected"
            else:
                # Extract brand from filename: date_City_viewport_ad_found_brand-name.png
                parts      = path.stem.split("_ad_found_")
                brand_part = parts[1].replace("-", " ").title() if len(parts) > 1 else "unknown brand"
                status     = f"✅ {brand_part}"
            file_uploads.append({
                "file": str(path),
                "filename": path.name,
                "title": f"{city} — {viewport} — {status}",
            })

        try:
            client.files_upload_v2(
                channel=channel,
                file_uploads=file_uploads,
                initial_comment=(
                    f":camera_with_flash: *Flipp Campaign Placements — {city} — {date_str}*\n"
                    f"Brands: `{brand_text}` | Screens: {len(paths)}"
                ),
            )
            print(f"  ✓ Uploaded {len(paths)} file(s) for {city} → {channel}")
        except SlackApiError as e:
            print(f"  ✗ Slack error for {city}: {e.response['error']}")


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run(brands: list[str], output_dir: Path, slack_channel: str, slack_token: str, max_attempts: int) -> None:
    date_str = datetime.now().strftime("%Y-%m-%d")
    run_dir  = output_dir / date_str
    run_dir.mkdir(parents=True, exist_ok=True)

    all_screenshots: list[Path] = []
    results: list[dict] = []

    async with async_playwright() as pw:
        for viewport_name, vp_config in VIEWPORTS.items():
            print(f"\n{'─'*52}")
            print(f"  Viewport: {viewport_name.upper()}  ({vp_config['width']}×{vp_config['height']})")
            print(f"{'─'*52}")

            browser = await pw.chromium.launch(headless=True)

            for city, postal_code in CITIES.items():
                print(f"\n  📍 {city} ({postal_code})")

                context = await browser.new_context(
                    viewport={"width": vp_config["width"], "height": vp_config["height"]},
                    is_mobile=vp_config["is_mobile"],
                    device_scale_factor=vp_config["device_scale_factor"],
                    user_agent=vp_config["user_agent"],
                    locale="en-CA",
                )

                path, ad_found, detected_brand = await capture_until_ad(
                    context, city, postal_code, viewport_name,
                    run_dir, date_str, max_attempts, brands,
                )

                if path:
                    all_screenshots.append(path)
                    results.append({
                        "city": city,
                        "viewport": viewport_name,
                        "ad_found": ad_found,
                        "brand": detected_brand,
                    })

                await context.close()

            await browser.close()

    # Summary table
    print(f"\n{'═'*58}")
    print(f"  {'City':<12} {'Viewport':<10} {'Ad Found?':<10} {'Brand'}")
    print(f"  {'────':<12} {'────────':<10} {'─────────':<10} {'─────'}")
    for r in results:
        icon  = "✅ Yes   " if r["ad_found"] else "⚠️  No    "
        brand = r["brand"] or ("unknown" if r["ad_found"] else "—")
        print(f"  {r['city']:<12} {r['viewport']:<10} {icon} {brand}")
    print(f"{'═'*58}")
    print(f"  {len(all_screenshots)} screenshots saved → {run_dir}\n")

    if slack_token and slack_channel:
        print("📤 Uploading to Slack…")
        upload_to_slack(all_screenshots, slack_channel, brands, date_str, slack_token)
    elif slack_channel and not slack_token:
        print("⚠  --slack-channel set but SLACK_BOT_TOKEN env var is missing.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture Flipp browse screen GAM campaign placements — desktop & mobile — Vancouver, Toronto, Montreal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python flipp_screenshots.py --brands "Coca-Cola, Nike, Lays"
  python flipp_screenshots.py --brands "Samsung" --attempts 20 --slack-channel "#brand-campaigns"

Environment variables:
  SLACK_BOT_TOKEN    Slack bot token (needs files:write + chat:write scopes)
  SLACK_CHANNEL      Default Slack channel (overridden by --slack-channel)
        """,
    )
    parser.add_argument("--brands", "-b", type=str, default="",
        help='Comma-separated brand names live today — included in Slack message')
    parser.add_argument("--output", "-o", type=str, default="./flipp_screenshots",
        help="Root output folder (default: ./flipp_screenshots)")
    parser.add_argument("--attempts", "-a", type=int, default=15,
        help="Max page reloads per city/viewport before giving up (default: 15)")
    parser.add_argument("--slack-channel", type=str,
        default=os.environ.get("SLACK_CHANNEL", ""),
        help="Slack channel to post to (e.g. #brand-campaigns)")

    args   = parser.parse_args()
    brands = [b.strip() for b in args.brands.split(",") if b.strip()]

    print("🎯 Flipp Campaign Screenshot Tool")
    print(f"   Cities   : Vancouver · Toronto · Montreal")
    print(f"   Brands   : {', '.join(brands) if brands else '(none specified)'}")
    print(f"   Attempts : up to {args.attempts} reloads per city/viewport")
    print(f"   Output   : {args.output}/{{date}}/")
    print(f"   Slack    : {args.slack_channel or '(not configured)'}")
    print()

    asyncio.run(run(
        brands=brands,
        output_dir=Path(args.output),
        slack_channel=args.slack_channel,
        slack_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        max_attempts=args.attempts,
    ))


if __name__ == "__main__":
    main()
