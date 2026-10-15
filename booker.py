"""
MATCHi auto-booker - Sundbybergs Rackethall, Badminton (Indoors), paid with punch card.

Two modes:
  * Normal  : look for the slot, book it, exit.
  * WATCH=1 : keep checking in the background (every CHECK_EVERY seconds) until a court
              frees up at TARGET_TIME, book it, notify you, exit.

Notifications when a booking succeeds (or the watcher gives up):
  * Windows pop-up notification + beep (always)
  * Phone push via the free ntfy app, if NTFY_TOPIC is set (install "ntfy" on your
    phone, subscribe to the same topic name - pick something hard to guess).

Usage (PowerShell), watch for 23 Oct 18:00 on any court:
  $env:MATCHI_USER="you@example.com"; $env:MATCHI_PASS="..."
  $env:TARGET_DATE="2026-10-23"; $env:TARGET_TIME="18:00"
  $env:WATCH="1"; $env:CHECK_EVERY="60"; $env:HEADLESS="1"
  $env:NTFY_TOPIC="shivi-badminton-x7k2"     # optional, for phone alerts
  python booker.py
"""

import asyncio
import datetime as dt
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------
USERNAME = os.getenv("MATCHI_USER", "")
PASSWORD = os.getenv("MATCHI_PASS", "")

TARGET_DATE = os.getenv("TARGET_DATE", "2026-10-23")   # YYYY-MM-DD
TARGET_TIME = os.getenv("TARGET_TIME", "18:00")        # slot start time
COURTS = [1, 2, 3, 4, 5, 6]                            # preference order
PUNCH_CARD_NAME = os.getenv("PUNCH_CARD", "Badmintonklippet")

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"             # "1" = stop before paying
OPEN_AT = os.getenv("OPEN_AT", "")                     # e.g. "2026-10-15 00:00:00"; empty = start now

WATCH = os.getenv("WATCH", "1") == "1"                 # keep watching until booked (default ON)
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "60" if WATCH else "2"))   # seconds between checks
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "20"))    # normal mode: give up after this long
STOP_BEFORE_MIN = int(os.getenv("STOP_BEFORE_MIN", "30"))  # watch mode: stop this many min before the slot starts
HEADLESS = os.getenv("HEADLESS", "0") == "1"           # "1" = no browser window
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "0"))       # >0 = stop this run after N minutes (GitHub Actions chunks)
NOTIFY_ERRORS = os.getenv("NOTIFY_ERRORS", "1") == "1" # "0" = don't push a notification for every failed run
STOP_FLAG = os.getenv("STOP_FLAG", "")                 # file written when the job is finished for good (booked / too late)

FACILITY = "sundbybergsrackethall"
BADMINTON_SPORT_ID = 2
HERE = Path(__file__).resolve().parent
# ----------------------------------------


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(HERE / "booker.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(title, message):
    """Windows toast + beep + optional phone push (ntfy). Never raises."""
    log(f"NOTIFY: {title} - {message}")
    # Phone push
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high", "Tags": "badminton"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"   (ntfy push failed: {e})")
    if sys.platform != "win32":
        return
    # Beep
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 300)
    except Exception:
        pass
    # Windows toast (uses Windows PowerShell 5.1, which can load the WinRT toast API)
    t = title.replace("'", "''")
    m = message.replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n = $x.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($x.CreateTextNode('{t}')) > $null;"
        f"$n.Item(1).AppendChild($x.CreateTextNode('{m}')) > $null;"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], timeout=20, capture_output=True)
    except Exception as e:
        log(f"   (toast failed: {e})")


async def dump(page, name):
    """Save screenshot + HTML so we can see exactly what the page looked like."""
    try:
        await page.screenshot(path=str(HERE / f"debug_{name}.png"), full_page=True)
        (HERE / f"debug_{name}.html").write_text(await page.content(), encoding="utf-8")
        log(f"   saved debug_{name}.png / .html")
    except Exception as e:
        log(f"   (could not save debug files: {e})")


async def click_by_text(scope, pattern, timeout_s=15):
    """Click the first visible button/link/input inside `scope` whose text or value matches `pattern`."""
    rx = re.compile(pattern, re.I)
    candidates = scope.locator("button, a, input[type=submit], input[type=button], label")
    end = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < end:
        n = await candidates.count()
        for i in range(n):
            el = candidates.nth(i)
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip() or (await el.get_attribute("value") or "")
                if rx.search(text):
                    log(f"   clicking: '{text}'")
                    await el.click()
                    return text
            except Exception:
                continue
        await asyncio.sleep(0.3)
    raise RuntimeError(f"No visible element matching /{pattern}/")


async def click_anywhere(context, pattern, timeout_s=30, selector="button, a, [role=button], input[type=submit]"):
    """Search every open tab and iframe for a visible element matching `pattern` and click it."""
    rx = re.compile(pattern, re.I)
    end = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < end:
        for pg in reversed(context.pages):
            for frame in pg.frames:
                try:
                    loc = frame.locator(selector).filter(has_text=rx)
                    for i in range(await loc.count()):
                        el = loc.nth(i)
                        if await el.is_visible() and await el.is_enabled():
                            text = " ".join((await el.inner_text()).split())
                            log(f"   clicking: '{text[:60]}'")
                            await el.scroll_into_view_if_needed()
                            await el.click()
                            return pg, text
                except Exception:
                    continue
        await asyncio.sleep(0.4)
    raise RuntimeError(f"No visible element matching /{pattern}/ on any page/frame")


FIND_SLOT_JS = """
([time, courts]) => {
  const cells = [...document.querySelectorAll('#schedule-table td.slot.free')];
  for (const c of courts) {
    for (const td of cells) {
      const t = (td.getAttribute('data-original-title') || td.getAttribute('title') || '')
                  .replace(/<br\\s*\\/?>/gi, '|');
      if (new RegExp('(^|\\\\|)\\\\s*' + c + '\\\\. Badminton').test(t) && t.includes(time + ' -')) {
        return { slotId: td.getAttribute('slotid'), label: t };
      }
    }
  }
  return null;
}
"""


async def login(page):
    log("Logging in...")
    await page.goto("https://www.matchi.se/login", wait_until="domcontentloaded")
    user_box = page.locator("input[name='username']")
    for attempt in range(6):                      # up to ~60s
        if await user_box.count() and await user_box.first.is_visible():
            break
        # Cookie / consent banner?
        for txt in ["Accept all", "Accept", "Godkänn alla", "Godkänn", "Acceptera alla", "Acceptera", "OK"]:
            btn = page.get_by_role("button", name=txt, exact=True)
            try:
                if await btn.count() and await btn.first.is_visible():
                    log(f"   clicking cookie button '{txt}'")
                    await btn.first.click()
                    break
            except Exception:
                pass
        body = ""
        try:
            body = (await page.locator("body").inner_text())[:200].replace("\n", " ")
        except Exception:
            pass
        log(f"   login form not visible yet (title='{await page.title()}', url={page.url}, text='{body}')")
        await page.wait_for_timeout(10000)
    await page.fill("input[name='username']", USERNAME)
    await page.fill("input[name='password']", PASSWORD)
    await page.click("button[type='submit']")
    await page.wait_for_selector("a[href*='/profile']", timeout=20000)
    log("   logged in")


async def ensure_logged_in(page):
    """Sessions expire over hours of watching - log in again if needed."""
    if await page.locator("a[href*='/profile']").count() == 0:
        log("   session expired - logging in again")
        await login(page)
        await load_schedule(page)


async def load_schedule(page):
    url = f"https://www.matchi.se/facilities/{FACILITY}?date={TARGET_DATE}&sport={BADMINTON_SPORT_ID}"
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_function(
            "document.querySelector('#schedule-table') || /no more than \\d+ days/i.test(document.querySelector('#schedule')?.innerText || '')",
            timeout=15000,
        )
    except Exception:
        pass
    active = page.locator("#sportTabs li.active")
    if await active.count() and "badminton" not in (await active.first.inner_text()).lower():
        await page.locator(f"#sportTabs a[href*='sport={BADMINTON_SPORT_ID}&']").first.click()
        await page.wait_for_selector("#schedule-table", timeout=15000)


async def book_slot(page, context, found):
    """Click the slot and go through Next -> punch card -> Pay. Returns True if confirmed."""
    log(f"Booking: {found['label']}")
    await page.locator(f"#schedule-table td.slot[slotid='{found['slotId']}']").click()

    modal = page.locator("#userBookingModal")
    await modal.wait_for(state="visible", timeout=15000)
    await page.wait_for_timeout(800)
    await click_by_text(modal, r"^\s*(next|nästa|gå vidare|continue|fortsätt)\s*$")

    # Checkout: ensure a punch card is chosen
    await page.wait_for_timeout(1500)
    pay_rx = r"pay with \d+ punch|betala med \d+ klipp"
    pay_seen = False
    for pg in reversed(context.pages):
        for frame in pg.frames:
            try:
                if await frame.get_by_text(re.compile(pay_rx, re.I)).first.is_visible():
                    pay_seen = True
            except Exception:
                pass
    if not pay_seen:
        clicked = False
        for pg in reversed(context.pages):
            for frame in pg.frames:
                if clicked:
                    break
                try:
                    opt = frame.get_by_text(PUNCH_CARD_NAME, exact=True).first
                    if await opt.count() and await opt.is_visible():
                        await opt.click()
                        log("   punch card option clicked")
                        clicked = True
                except Exception:
                    pass

    if DRY_RUN:
        log("DRY RUN - not clicking Pay.")
        await dump(context.pages[-1], "ready_to_pay")
        return False

    # Only ever clicks a button mentioning punch/klipp - never card/Swish
    pay_page, _ = await click_anywhere(context, pay_rx, timeout_s=30)
    try:
        await pay_page.wait_for_function(
            "/(booking (is |has been )?confirmed|thank you|thanks for|your booking|bokningen är|tack för|bekräftad)/i.test(document.body.innerText)",
            timeout=20000,
        )
        confirmed = True
    except Exception:
        confirmed = False
    await dump(pay_page, "after_pay")
    return confirmed


async def close_extra_pages(context, keep):
    for pg in list(context.pages):
        if pg is not keep:
            try:
                await pg.close()
            except Exception:
                pass


def keep_awake(on=True):
    """Stop Windows from going to sleep while the script runs (screen may still turn off / lock)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        if on:
            log("Keeping the PC awake while watching (locking the screen is fine)")
    except Exception as e:
        log(f"   (could not block sleep: {e})")


async def run():
    if not USERNAME or not PASSWORD:
        raise SystemExit("Set MATCHI_USER and MATCHI_PASS environment variables first.")
    keep_awake(True)

    slot_start = dt.datetime.strptime(f"{TARGET_DATE} {TARGET_TIME}", "%Y-%m-%d %H:%M")
    if WATCH:
        final_deadline = slot_start - dt.timedelta(minutes=STOP_BEFORE_MIN)
    else:
        final_deadline = dt.datetime.now() + dt.timedelta(minutes=POLL_MINUTES)
    deadline = final_deadline
    if RUN_MINUTES > 0:
        deadline = min(final_deadline, dt.datetime.now() + dt.timedelta(minutes=RUN_MINUTES))

    def finished_for_good():
        if STOP_FLAG:
            Path(STOP_FLAG).write_text("done", encoding="utf-8")

    if dt.datetime.now() >= final_deadline:
        log("Past the watch deadline - nothing to do.")
        finished_for_good()
        return

    log(f"Target {TARGET_DATE} {TARGET_TIME}, courts {COURTS}, WATCH={WATCH}, every {CHECK_EVERY}s, "
        f"until {deadline:%Y-%m-%d %H:%M}, DRY_RUN={DRY_RUN}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-GB",
            timezone_id="Europe/Stockholm",
            # Normal desktop Chrome identity - headless Chrome otherwise says "HeadlessChrome", which sites block
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        try:
            await login(page)

            if OPEN_AT:
                open_at = dt.datetime.strptime(OPEN_AT, "%Y-%m-%d %H:%M:%S")
                while (wait := (open_at - dt.datetime.now()).total_seconds()) > 0:
                    log(f"Waiting {wait:.0f}s until {OPEN_AT} ...")
                    await asyncio.sleep(min(wait, 60))

            checks = 0
            while dt.datetime.now() < deadline:
                checks += 1
                try:
                    await load_schedule(page)
                    await ensure_logged_in(page)
                    found = await page.evaluate(FIND_SLOT_JS, [TARGET_TIME, COURTS])
                except Exception as e:
                    log(f"   check failed ({e}) - will retry")
                    found = None

                if not found:
                    if checks == 1 or checks % 10 == 0:
                        log(f"   no free {TARGET_TIME} court yet (check #{checks}) - next check in {CHECK_EVERY}s")
                    await asyncio.sleep(CHECK_EVERY)
                    continue

                try:
                    ok = await book_slot(page, context, found)
                except Exception as e:
                    # Someone may have grabbed it first - keep watching
                    log(f"   booking attempt failed: {e}")
                    await dump(context.pages[-1], "attempt_failed")
                    await close_extra_pages(context, page)
                    await asyncio.sleep(5)
                    continue

                if DRY_RUN:
                    notify("MATCHi dry run", f"Reached Pay for {found['label'].replace('|', ' ')}")
                    return
                court = found["label"].split("|")[1].strip()
                finished_for_good()   # never try to book a second court
                if ok:
                    notify("Badminton booked!", f"{court}, {TARGET_DATE} {TARGET_TIME} (1 punch)")
                else:
                    notify("MATCHi: check booking", f"Pay clicked for {court} {TARGET_DATE} {TARGET_TIME} but no confirmation seen - check My bookings")
                return

            if dt.datetime.now() >= final_deadline:
                finished_for_good()
                notify("MATCHi watcher stopped", f"No free {TARGET_TIME} court on {TARGET_DATE} - gave up at {final_deadline:%H:%M}")
            else:
                log(f"This run's time is up after {checks} checks - the next scheduled run continues.")

        except Exception as e:
            log(f"FAILED: {e}")
            await dump(page, "error")
            if NOTIFY_ERRORS:
                notify("MATCHi bot error", str(e)[:150])
            raise
        finally:
            keep_awake(False)
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
