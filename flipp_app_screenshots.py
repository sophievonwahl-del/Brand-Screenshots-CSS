#!/usr/bin/env python3
"""
Flipp App Campaign Screenshot Tool (Android)
=============================================
Controls the Flipp Android app in an emulator via Appium.
For each city, sets the postal code, reloads the browse screen up to
MAX_ATTEMPTS times until a GAM ad placement is detected, then saves a
full screenshot. Runs both portrait (phone) and landscape (tablet) if needed.

One-time setup
--------------
1. Install Android Studio: https://developer.android.com/studio
2. In Android Studio → Virtual Device Manager → Create Device
   - Hardware: Pixel 6  (or any phone)
   - System image: API 33, ABI x86_64, Target: "Google Play" (not just "Google APIs")
   - Name it: Pixel_6_API33  (used in AVD_NAME below)
3. Start the emulator, sign in to Google, install Flipp from Play Store, and log in
4. Install Node + Appium:
     brew install node
     npm install -g appium
     appium driver install uiautomator2
5. Install Python dependencies:
     pip install Appium-Python-Client slack-sdk

Usage
-----
    # Start the Appium server in a separate terminal first:
    appium

    # Then run this script:
    python flipp_app_screenshots.py --brands "Coca-Cola, Nike"
    python flipp_app_screenshots.py --brands "Samsung" --attempts 20 --slack-channel "#brand-campaigns"

Environment variables
---------------------
    SLACK_BOT_TOKEN   Slack bot token (files:write + chat:write scopes)
    SLACK_CHANNEL     Default Slack channel
    APPIUM_HOST       Appium server host (default: 127.0.0.1)
    APPIUM_PORT       Appium server port (default: 4723)
"""

import argparse
import os
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path

try:
    from appium import webdriver
    from appium.options import UiAutomator2Options
    from appium.webdriver.common.appiumby import AppiumBy
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except ImportError:
    print("❌ Missing dependency: Appium-Python-Client")
    print("   Run: pip install Appium-Python-Client")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

CITIES = {
    "Vancouver": "V6B 1A1",
    "Toronto":   "M5V 2T6",
    "Montreal":  "H3A 1A1",
}

# Your emulator AVD name (check: emulator -list-avds)
AVD_NAME = "Pixel_6_API33"

# Flipp Android package — verify with: adb shell pm list packages | grep flipp
FLIPP_PACKAGE  = "com.wishabi.flipp"
FLIPP_ACTIVITY = "com.wishabi.flipp.home.HomeActivity"

APPIUM_HOST = os.environ.get("APPIUM_HOST", "127.0.0.1")
APPIUM_PORT = os.environ.get("APPIUM_PORT", "4723")

# Seconds to wait for the browse screen to settle after a restart
SETTLE_SECS = 6

# GAM AdView class used by Google Mobile Ads SDK in Android
GAM_AD_CLASSES = [
    "com.google.android.gms.ads.AdView",
    "com.google.android.gms.ads.admanager.AdManagerAdView",
]

# Custom targeting keys to check for brand info (via logcat / ad request)
BRAND_TARGETING_KEYS = ["brand", "advertiser", "client", "campaign", "sponsor"]


# ── Emulator helpers ──────────────────────────────────────────────────────────

def start_emulator(avd_name: str) -> None:
    """Launch the emulator if it isn't already running."""
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    if "emulator" in result.stdout:
        print("  ✓ Emulator already running")
        return

    print(f"  → Starting emulator: {avd_name}…")
    subprocess.Popen(
        ["emulator", "-avd", avd_name, "-no-snapshot-load"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for device to be ready
    subprocess.run(["adb", "wait-for-device"], timeout=120)
    # Wait for boot to complete
    for _ in range(60):
        r = subprocess.run(
            ["adb", "shell", "getprop", "sys.boot_completed"],
            capture_output=True, text=True,
        )
        if r.stdout.strip() == "1":
            break
        time.sleep(2)
    print("  ✓ Emulator ready")


def make_driver() -> webdriver.Remote:
    """Create and return an Appium driver connected to the Flipp app."""
    options = UiAutomator2Options()
    options.platform_name          = "Android"
    options.automation_name        = "UiAutomator2"
    options.app_package            = FLIPP_PACKAGE
    options.app_activity           = FLIPP_ACTIVITY
    options.no_reset               = True   # keep login + location between runs
    options.full_reset             = False
    options.auto_grant_permissions = True
    options.new_command_timeout    = 120

    return webdriver.Remote(
        f"http://{APPIUM_HOST}:{APPIUM_PORT}",
        options=options,
    )


# ── Location setter ───────────────────────────────────────────────────────────

def set_location(driver: webdriver.Remote, postal_code: str) -> bool:
    """
    Navigate to the location/postal code setting in Flipp and enter the code.
    Returns True on success. Falls back gracefully if the UI changed.

    NOTE: If Flipp updates its UI, use Appium Inspector
    (https://github.com/appium/appium-inspector) to find the correct
    element resource-ids and update the selectors below.
    """
    wait = WebDriverWait(driver, 8)

    # Try common patterns for a location/postal code button
    location_selectors = [
        # Resource ID patterns (update these after inspecting with Appium Inspector)
        (AppiumBy.ID,              "com.wishabi.flipp:id/postal_code"),
        (AppiumBy.ID,              "com.wishabi.flipp:id/location"),
        (AppiumBy.ID,              "com.wishabi.flipp:id/location_button"),
        (AppiumBy.ID,              "com.wishabi.flipp:id/postal_code_input"),
        # Accessibility label fallbacks
        (AppiumBy.ACCESSIBILITY_ID, "Set your location"),
        (AppiumBy.ACCESSIBILITY_ID, "Change location"),
        (AppiumBy.ACCESSIBILITY_ID, "Enter postal code"),
        # Text-based fallback
        (AppiumBy.ANDROID_UIAUTOMATOR,
         'new UiSelector().textContains("postal").className("android.widget.EditText")'),
        (AppiumBy.ANDROID_UIAUTOMATOR,
         'new UiSelector().textContains("location").className("android.widget.Button")'),
    ]

    for by, selector in location_selectors:
        try:
            el = wait.until(EC.presence_of_element_located((by, selector)))
            el.click()
            time.sleep(1)

            # Now look for the text input that appeared
            input_el = driver.find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().className("android.widget.EditText").focused(true)',
            )
            input_el.clear()
            input_el.send_keys(postal_code)

            # Confirm — try pressing Enter or tapping a "Done"/"Go" button
            input_el.submit()
            time.sleep(2)
            return True
        except Exception:
            continue

    print(f"    ⚠ Could not set location to {postal_code} — using current location")
    return False


def navigate_to_browse(driver: webdriver.Remote) -> None:
    """Tap the Browse / Flyers tab to land on the browse screen."""
    browse_selectors = [
        (AppiumBy.ID,               "com.wishabi.flipp:id/tab_browse"),
        (AppiumBy.ID,               "com.wishabi.flipp:id/nav_flyers"),
        (AppiumBy.ACCESSIBILITY_ID, "Browse"),
        (AppiumBy.ACCESSIBILITY_ID, "Flyers"),
        (AppiumBy.ANDROID_UIAUTOMATOR,
         'new UiSelector().text("Browse")'),
        (AppiumBy.ANDROID_UIAUTOMATOR,
         'new UiSelector().text("Flyers")'),
    ]
    for by, sel in browse_selectors:
        try:
            el = driver.find_element(by, sel)
            el.click()
            time.sleep(2)
            return
        except Exception:
            continue


# ── GAM ad detection ──────────────────────────────────────────────────────────

def gam_ad_is_rendered(driver: webdriver.Remote) -> bool:
    """
    Return True if a rendered GAM AdView is visible on screen.
    Google Mobile Ads SDK uses com.google.android.gms.ads.AdView
    (or AdManagerAdView for GAM specifically).
    """
    for cls in GAM_AD_CLASSES:
        try:
            elements = driver.find_elements(AppiumBy.CLASS_NAME, cls)
            for el in elements:
                if el.is_displayed():
                    size = el.size
                    if size["width"] > 0 and size["height"] > 0:
                        return True
        except Exception:
            continue
    return False


def detect_brand_from_logcat(known_brands: list[str]) -> str | None:
    """
    Read recent logcat output and look for GAM ad request URLs containing
    brand targeting params (cust_params). This works because Android logs
    network activity from the GAM SDK.
    """
    try:
        result = subprocess.run(
            ["adb", "logcat", "-d", "-t", "500", "-s", "Ads:V", "GAM:V", "google:V"],
            capture_output=True, text=True, timeout=5,
        )
        log = result.stdout

        brand_lower = {b.lower(): b for b in known_brands}

        import urllib.parse
        for line in log.splitlines():
            if "cust_params" not in line and "doubleclick" not in line:
                continue
            # Try to extract cust_params value
            for part in line.split():
                if "cust_params" in part:
                    try:
                        raw = urllib.parse.unquote(part.split("cust_params=")[-1].split("&")[0])
                        for kv in raw.split("&"):
                            if "=" in kv:
                                k, v = kv.split("=", 1)
                                if k.lower() in BRAND_TARGETING_KEYS:
                                    for bl, original in brand_lower.items():
                                        if bl in v.lower() or v.lower() in bl:
                                            return original
                                    return v.strip()
                    except Exception:
                        continue

        # Broader fallback: just check log text for known brand names
        for bl, original in brand_lower.items():
            if bl in log.lower():
                return original

    except Exception:
        pass

    return None


# ── Core capture loop ─────────────────────────────────────────────────────────

def capture_until_ad(
    city: str,
    postal_code: str,
    output_dir: Path,
    date_str: str,
    max_attempts: int,
    known_brands: list[str],
) -> tuple[Path | None, bool, str | None]:
    """
    Launch Flipp, set location, and reload the browse screen until a GAM ad
    is detected or max_attempts is reached. Returns (path, ad_found, brand).
    """
    print(f"    → Connecting to Appium & launching Flipp…")
    try:
        driver = make_driver()
    except Exception as e:
        print(f"    ✗ Could not connect to Appium: {e}")
        print("      Is `appium` running? Start it with: appium")
        return None, False, None

    ad_found       = False
    detected_brand: str | None = None

    try:
        time.sleep(SETTLE_SECS)

        # Navigate to browse tab
        navigate_to_browse(driver)

        # Set location
        print(f"    → Setting location: {postal_code}")
        set_location(driver, postal_code)
        time.sleep(SETTLE_SECS)

        # Clear logcat before we start so we only see fresh ad requests
        subprocess.run(["adb", "logcat", "-c"], capture_output=True)

        for attempt in range(1, max_attempts + 1):
            print(f"    → Attempt {attempt}/{max_attempts} — checking for GAM ad…", end=" ", flush=True)

            if gam_ad_is_rendered(driver):
                detected_brand = detect_brand_from_logcat(known_brands)
                brand_label    = f" ({detected_brand})" if detected_brand else ""
                print(f"✓ Ad detected!{brand_label}")
                ad_found = True
                break
            else:
                print("no ad, restarting app…")
                # Terminate and relaunch to get a fresh ad auction
                driver.terminate_app(FLIPP_PACKAGE)
                time.sleep(1)
                driver.activate_app(FLIPP_PACKAGE)
                time.sleep(SETTLE_SECS)
                navigate_to_browse(driver)
                time.sleep(2)
                # Clear logcat again for fresh read
                subprocess.run(["adb", "logcat", "-c"], capture_output=True)

        # Build filename
        brand_slug = f"_{detected_brand.lower().replace(' ', '-')}" if detected_brand else ""
        suffix     = f"ad_found{brand_slug}" if ad_found else "no_ad"
        filename   = f"{date_str}_{city.replace(' ', '_')}_android_{suffix}.png"
        out_path   = output_dir / filename

        # Scroll to top to ensure banner is visible, then screenshot
        try:
            driver.execute_script("mobile: scroll", {"direction": "up"})
            time.sleep(1)
        except Exception:
            pass

        driver.save_screenshot(str(out_path))
        print(f"    {'✅' if ad_found else '⚠️ '} Saved: {out_path.name}")

    except Exception as e:
        print(f"    ✗ Error: {e}")
        out_path = None

    finally:
        try:
            driver.quit()
        except Exception:
            pass

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
        print("  ⚠  slack-sdk not installed — skipping. Run: pip install slack-sdk")
        return

    client     = WebClient(token=token)
    brand_text = ", ".join(brands) if brands else "all placements"

    by_city: dict[str, list[Path]] = {}
    for p in screenshots:
        parts = p.stem.split("_")
        city  = parts[1] if len(parts) >= 2 else "Unknown"
        by_city.setdefault(city, []).append(p)

    for city, paths in by_city.items():
        file_uploads = []
        for path in paths:
            if "no_ad" in path.stem:
                status = "⚠️ No ad detected"
            else:
                parts      = path.stem.split("_ad_found_")
                brand_part = parts[1].replace("-", " ").title() if len(parts) > 1 else "unknown brand"
                status     = f"✅ {brand_part}"
            file_uploads.append({
                "file": str(path),
                "filename": path.name,
                "title": f"{city} — Android — {status}",
            })
        try:
            client.files_upload_v2(
                channel=channel,
                file_uploads=file_uploads,
                initial_comment=(
                    f":iphone: *Flipp App Placements — {city} — {date_str}*\n"
                    f"Brands: `{brand_text}` | Screens: {len(paths)}"
                ),
            )
            print(f"  ✓ Uploaded {len(paths)} file(s) for {city} → {channel}")
        except SlackApiError as e:
            print(f"  ✗ Slack error for {city}: {e.response['error']}")


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run(brands: list[str], output_dir: Path, slack_channel: str, slack_token: str, max_attempts: int) -> None:
    date_str = datetime.now().strftime("%Y-%m-%d")
    run_dir  = output_dir / date_str
    run_dir.mkdir(parents=True, exist_ok=True)

    # Start emulator if needed
    print("\n📱 Checking emulator…")
    start_emulator(AVD_NAME)

    all_screenshots: list[Path] = []
    results: list[dict]         = []

    for city, postal_code in CITIES.items():
        print(f"\n{'─'*52}")
        print(f"  📍 {city} ({postal_code})")
        print(f"{'─'*52}")

        path, ad_found, detected_brand = capture_until_ad(
            city, postal_code, run_dir, date_str, max_attempts, brands,
        )

        if path:
            all_screenshots.append(path)
        results.append({
            "city": city, "ad_found": ad_found, "brand": detected_brand,
        })

    # Summary
    print(f"\n{'═'*58}")
    print(f"  {'City':<12} {'Ad Found?':<12} {'Brand'}")
    print(f"  {'────':<12} {'─────────':<12} {'─────'}")
    for r in results:
        icon  = "✅ Yes     " if r["ad_found"] else "⚠️  No      "
        brand = r["brand"] or ("unknown" if r["ad_found"] else "—")
        print(f"  {r['city']:<12} {icon} {brand}")
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
        description="Capture Flipp Android app browse screen campaign placements — Vancouver, Toronto, Montreal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--brands", "-b", type=str, default="",
        help="Comma-separated brand names live today")
    parser.add_argument("--output", "-o", type=str, default="./flipp_screenshots",
        help="Root output folder (default: ./flipp_screenshots)")
    parser.add_argument("--attempts", "-a", type=int, default=15,
        help="Max app restarts per city before giving up (default: 15)")
    parser.add_argument("--slack-channel", type=str,
        default=os.environ.get("SLACK_CHANNEL", ""),
        help="Slack channel to post to (e.g. #brand-campaigns)")
    parser.add_argument("--avd", type=str, default=AVD_NAME,
        help=f"Android emulator AVD name (default: {AVD_NAME})")

    args   = parser.parse_args()
    brands = [b.strip() for b in args.brands.split(",") if b.strip()]

    # Allow overriding AVD name from CLI
    global AVD_NAME
    AVD_NAME = args.avd

    print("🎯 Flipp App Screenshot Tool  (Android)")
    print(f"   Cities   : Vancouver · Toronto · Montreal")
    print(f"   Brands   : {', '.join(brands) if brands else '(none specified)'}")
    print(f"   Attempts : up to {args.attempts} restarts per city")
    print(f"   Emulator : {AVD_NAME}")
    print(f"   Output   : {args.output}/{{date}}/")
    print(f"   Slack    : {args.slack_channel or '(not configured)'}")
    print()

    run(
        brands=brands,
        output_dir=Path(args.output),
        slack_channel=args.slack_channel,
        slack_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        max_attempts=args.attempts,
    )


if __name__ == "__main__":
    main()
