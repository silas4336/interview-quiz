#!/usr/bin/env python3
"""
TableCheck 自動訂位機器人
=========================

用 Playwright 驅動真正的訂位頁面（跟真人操作一樣），在每天開放訂位的
時刻（預設日本時間 21:00）準時喚醒、用快速輪詢搶下指定時段。

用法
----
    # 1) 安裝
    pip install -r requirements.txt
    playwright install chromium

    # 2) 設定
    cp config.example.yaml config.yaml   # 然後編輯 config.yaml

    # 3) 先手動登入一次（session 會被記住）
    python bot.py --login

    # 4) 排程搶位：程式會一直等到下一個開放時刻才動作
    python bot.py

    # 其他好用選項：
    python bot.py --now        # 不等待，立刻嘗試一次（測試用）
    python bot.py --inspect    # 開啟頁面並暫停，方便你檢查/校正選擇器
    python bot.py --headful    # 顯示瀏覽器視窗（預設搶位時也是有頭模式）

注意
----
* 自動化操作可能違反 TableCheck 服務條款，使用風險自負，請勿濫用、
  也不要把輪詢間隔調到太誇張而對伺服器造成負擔。
* 這家店每人有不可退訂金，送出訂位等於要付款，請理解後再使用。
* 網站前端改版時，下方選擇器可能需要微調（用 --inspect 協助校正）。
"""

import argparse
import datetime as dt
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import (
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

HERE = Path(__file__).resolve().parent
# 持久化的瀏覽器使用者資料夾——登入 session 存在這裡，搶位時免再登入。
USER_DATA_DIR = HERE / ".browser-profile"


# ---------------------------------------------------------------------------
# 設定載入
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"找不到設定檔 {path}\n"
            f"請先執行: cp {HERE/'config.example.yaml'} {path} 並編輯內容。"
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("locale", "en")
    cfg.setdefault("timezone", "Asia/Tokyo")
    cfg.setdefault("warmup_seconds", 20)
    cfg.setdefault("attempt_window_seconds", 90)
    cfg.setdefault("target_days_ahead", 7)
    cfg.setdefault("target_date", "")
    cfg.setdefault("accept_any_time", False)
    cfg.setdefault("stop_before_payment", True)
    cfg.setdefault("notify", {})
    if not cfg.get("shop_slug"):
        sys.exit("config.yaml 缺少 shop_slug")
    return cfg


def reserve_url(cfg: dict) -> str:
    return f"https://www.tablecheck.com/{cfg['locale']}/shops/{cfg['shop_slug']}/reserve"


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 時間：算出下一個開放時刻並等待
# ---------------------------------------------------------------------------
def next_open_datetime(cfg: dict) -> dt.datetime:
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    h, m, s = (int(x) for x in cfg["open_time"].split(":"))
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def sleep_until(target: dt.datetime, wake_before_seconds: int) -> None:
    """睡到 (target - wake_before_seconds)，分段睡眠以便顯示倒數。"""
    tz = target.tzinfo
    wake_at = target - dt.timedelta(seconds=wake_before_seconds)
    while True:
        now = dt.datetime.now(tz)
        remaining = (wake_at - now).total_seconds()
        if remaining <= 0:
            return
        if remaining > 120:
            log(f"距離暖機還有 {int(remaining)} 秒（開放時刻 {target:%Y-%m-%d %H:%M:%S %Z}）")
            time.sleep(min(remaining - 60, 300))
        else:
            log(f"暖機倒數 {int(remaining)} 秒...")
            time.sleep(min(remaining, 5))


def busy_wait_until(target: dt.datetime) -> None:
    """最後一哩用 busy-wait 確保準時（毫秒級）。"""
    tz = target.tzinfo
    while dt.datetime.now(tz) < target:
        time.sleep(0.01)


def target_date_str(cfg: dict) -> str:
    if cfg.get("target_date"):
        return cfg["target_date"]
    tz = ZoneInfo(cfg["timezone"])
    d = dt.datetime.now(tz).date() + dt.timedelta(days=int(cfg["target_days_ahead"]))
    return d.isoformat()


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def notify(cfg: dict, message: str) -> None:
    log(f"🔔 {message}")
    nd = cfg.get("notify") or {}
    if nd.get("beep"):
        # 終端機響鈴
        sys.stdout.write("\a")
        sys.stdout.flush()
    cmd = nd.get("command")
    if cmd:
        import subprocess

        try:
            subprocess.Popen(cmd, shell=True)
        except Exception as e:  # noqa: BLE001
            log(f"通知指令執行失敗: {e}")


# ---------------------------------------------------------------------------
# 頁面操作
#
# TableCheck 的訂位前端是 React widget，class 名稱常變動，因此這裡盡量用
# 「角色 / 可見文字」的選擇器（Playwright 的 get_by_role / get_by_text），
# 對改版較有抵抗力。若仍失效，用 `python bot.py --inspect` 校正。
# ---------------------------------------------------------------------------
def maybe_dismiss_cookie(page: Page) -> None:
    for label in ("Accept", "同意", "OK", "Got it", "Agree"):
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=1000)
                log(f"關閉 cookie/同意彈窗 ({label})")
                return
        except Exception:  # noqa: BLE001
            pass


def select_party_size(page: Page, size: int) -> bool:
    """選擇人數。常見為數字按鈕或下拉選單。"""
    # 1) 直接點數字按鈕
    for getter in (
        lambda: page.get_by_role("button", name=str(size), exact=True),
        lambda: page.get_by_role("button", name=f"{size} people", exact=False),
        lambda: page.get_by_role("button", name=f"{size} guests", exact=False),
    ):
        try:
            el = getter()
            if el.count() and el.first.is_visible():
                el.first.click(timeout=1500)
                log(f"已選人數: {size}")
                return True
        except Exception:  # noqa: BLE001
            pass
    # 2) 下拉選單
    try:
        combo = page.get_by_role("combobox").first
        if combo.count():
            combo.select_option(str(size), timeout=1500)
            log(f"已選人數(下拉): {size}")
            return True
    except Exception:  # noqa: BLE001
        pass
    log("⚠️ 找不到人數選擇器，請用 --inspect 校正")
    return False


def select_date(page: Page, date_iso: str) -> bool:
    """在日曆/日期選擇器選擇目標日期。
    嘗試多種常見呈現：data 屬性、aria-label、可見日數字。"""
    d = dt.date.fromisoformat(date_iso)
    selectors = [
        f'[data-date="{date_iso}"]',
        f'[aria-label*="{date_iso}"]',
        f'button[aria-label*="{d.strftime("%B %-d")}"]',  # e.g. "June 27"
        f'button[aria-label*="{d.strftime("%b %-d")}"]',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel)
            if el.count() and el.first.is_enabled():
                el.first.click(timeout=1500)
                log(f"已選日期: {date_iso}")
                return True
        except Exception:  # noqa: BLE001
            pass
    # 退而求其次：點當月日曆上「日」的數字（需自行確認月份正確）
    try:
        day_btn = page.get_by_role("button", name=str(d.day), exact=True)
        if day_btn.count() and day_btn.first.is_enabled():
            day_btn.first.click(timeout=1500)
            log(f"已選日期(日數字): {date_iso}")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def pick_time_slot(page: Page, cfg: dict) -> str | None:
    """依偏好順序嘗試點擊時段；成功回傳時段文字。"""
    prefs = list(cfg.get("preferred_times") or [])
    for t in prefs:
        for getter in (
            lambda t=t: page.get_by_role("button", name=t, exact=False),
            lambda t=t: page.get_by_text(t, exact=False),
        ):
            try:
                el = getter()
                if el.count() and el.first.is_visible() and el.first.is_enabled():
                    el.first.click(timeout=1200)
                    log(f"✅ 點到時段: {t}")
                    return t
            except Exception:  # noqa: BLE001
                pass
    if cfg.get("accept_any_time"):
        # 抓任何看起來像時間的可點按鈕（HH:MM）
        try:
            cand = page.locator("button:enabled").filter(has_text="").all()
            import re

            for el in cand:
                try:
                    txt = (el.inner_text() or "").strip()
                    if re.match(r"^\d{1,2}[:：]\d{2}", txt) and el.is_visible():
                        el.click(timeout=1200)
                        log(f"✅ 點到時段(任意): {txt}")
                        return txt
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    return None


def fill_personal_details(page: Page, cfg: dict) -> None:
    """若未登入或欄位為空，嘗試填入聯絡資料。已登入通常會自動帶入。"""
    fields = {
        "name": cfg.get("name", ""),
        "email": cfg.get("email", ""),
        "phone": cfg.get("phone", ""),
    }
    for key, val in fields.items():
        if not val:
            continue
        for sel in (f'input[name*="{key}"]', f'input[type="{key}"]', f'input[id*="{key}"]'):
            try:
                el = page.locator(sel)
                if el.count() and el.first.is_visible() and not el.first.input_value():
                    el.first.fill(val, timeout=1000)
                    break
            except Exception:  # noqa: BLE001
                pass


def proceed_next(page: Page) -> bool:
    """點擊 下一步 / 繼續 / 確認 之類的前進按鈕。"""
    for label in ("Next", "Continue", "Confirm", "Book", "Reserve", "下一步", "繼續", "確認", "予約"):
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count() and btn.first.is_visible() and btn.first.is_enabled():
                btn.first.click(timeout=1500)
                log(f"前進: {label}")
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


# ---------------------------------------------------------------------------
# 主搶位流程
# ---------------------------------------------------------------------------
def attempt_booking(page: Page, cfg: dict) -> bool:
    url = reserve_url(cfg)
    date_iso = target_date_str(cfg)
    log(f"目標: {cfg['shop_slug']}  日期={date_iso}  人數={cfg['party_size']}")

    deadline = time.monotonic() + int(cfg["attempt_window_seconds"])
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            log(f"--- 第 {attempt} 次嘗試 ---")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            maybe_dismiss_cookie(page)

            select_party_size(page, int(cfg["party_size"]))

            if not select_date(page, date_iso):
                log("日期尚不可選（可能還沒開放），稍候重試...")
                time.sleep(0.5)
                continue

            # 等時段渲染（短等待，盡量快）
            page.wait_for_timeout(300)
            slot = pick_time_slot(page, cfg)
            if not slot:
                log("目標時段尚無空位，重試...")
                time.sleep(0.4)
                continue

            # 走到聯絡資料 / 確認頁
            fill_personal_details(page, cfg)
            proceed_next(page)
            page.wait_for_timeout(500)
            fill_personal_details(page, cfg)

            if cfg.get("stop_before_payment", True):
                notify(
                    cfg,
                    f"搶到時段 {slot}（{date_iso}）！已停在付款前，請立刻手動完成付款 💳",
                )
                return True

            # 自動一路送出（需帳號已綁付款方式）
            for _ in range(4):
                if not proceed_next(page):
                    break
                page.wait_for_timeout(700)
            notify(cfg, f"已嘗試自動送出時段 {slot}（{date_iso}），請至信箱/帳號確認結果。")
            return True

        except PWTimeout as e:
            log(f"逾時，重試: {e}")
            time.sleep(0.4)
        except Exception as e:  # noqa: BLE001
            log(f"錯誤，重試: {e}")
            time.sleep(0.4)

    notify(cfg, "可惜，這次沒搶到（時間窗結束）。")
    return False


# ---------------------------------------------------------------------------
# 進入點
# ---------------------------------------------------------------------------
def run_browser(cfg: dict, headless: bool):
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=headless,
        locale=cfg["locale"],
        timezone_id=cfg["timezone"],
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()
    return pw, context, page


def cmd_login(cfg: dict):
    log("開啟瀏覽器供你手動登入；登入完成後回到終端機按 Enter。")
    pw, context, page = run_browser(cfg, headless=False)
    page.goto(reserve_url(cfg), wait_until="domcontentloaded")
    input("登入完成後按 Enter 結束（session 會被記住）...")
    context.close()
    pw.stop()
    log("已儲存登入 session。")


def cmd_inspect(cfg: dict):
    pw, context, page = run_browser(cfg, headless=False)
    page.goto(reserve_url(cfg), wait_until="domcontentloaded")
    log("已開啟頁面。使用 Playwright Inspector 檢查選擇器；關閉視窗或按 Enter 結束。")
    try:
        page.pause()  # 開啟 Inspector（需有頭模式）
    except Exception:  # noqa: BLE001
        input("按 Enter 結束...")
    context.close()
    pw.stop()


def cmd_run(cfg: dict, run_now: bool, headless: bool):
    if not run_now:
        target = next_open_datetime(cfg)
        log(f"下一個開放時刻: {target:%Y-%m-%d %H:%M:%S %Z}")
        sleep_until(target, int(cfg["warmup_seconds"]))

    pw, context, page = run_browser(cfg, headless=headless)
    try:
        # 暖機：先把頁面載好、選好人數，等到整點再搶時段
        try:
            page.goto(reserve_url(cfg), wait_until="domcontentloaded", timeout=20000)
            maybe_dismiss_cookie(page)
            select_party_size(page, int(cfg["party_size"]))
            log("暖機完成，頁面已就緒。")
        except Exception as e:  # noqa: BLE001
            log(f"暖機載入失敗（會在搶位時重試）: {e}")

        if not run_now:
            target = next_open_datetime(cfg)
            # next_open_datetime 在 sleep 後可能已滾到隔天；用今天的目標
            tz = ZoneInfo(cfg["timezone"])
            h, m, s = (int(x) for x in cfg["open_time"].split(":"))
            today_target = dt.datetime.now(tz).replace(
                hour=h, minute=m, second=s, microsecond=0
            )
            if today_target < dt.datetime.now(tz) - dt.timedelta(minutes=5):
                today_target = target
            log("等待開放整點（busy-wait 精準對時）...")
            busy_wait_until(today_target)
            log("⏰ 開放！開始搶位")

        attempt_booking(cfg=cfg, page=page)

        if cfg.get("stop_before_payment", True):
            input("搶位流程結束。完成付款後按 Enter 關閉瀏覽器...")
    finally:
        context.close()
        pw.stop()


def main():
    ap = argparse.ArgumentParser(description="TableCheck 自動訂位機器人")
    ap.add_argument("--config", default=str(HERE / "config.yaml"), help="設定檔路徑")
    ap.add_argument("--login", action="store_true", help="開啟瀏覽器手動登入並記住 session")
    ap.add_argument("--inspect", action="store_true", help="開啟頁面並暫停，方便校正選擇器")
    ap.add_argument("--now", action="store_true", help="不等待，立刻嘗試一次")
    ap.add_argument("--headful", action="store_true", help="顯示瀏覽器視窗")
    ap.add_argument("--headless", action="store_true", help="無頭模式執行")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    if args.login:
        cmd_login(cfg)
        return
    if args.inspect:
        cmd_inspect(cfg)
        return

    # 搶位預設用有頭模式（較不易被判定為機器人，也方便你看狀況/付款）
    headless = args.headless and not args.headful
    cmd_run(cfg, run_now=args.now, headless=headless)


if __name__ == "__main__":
    main()
