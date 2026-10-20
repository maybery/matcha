#!/usr/bin/env python3
"""
丸久小山园(Marukyu Koyamaen)抹茶上货监测（基于列表页，不需要登录）

逻辑：
1. 抓取抹茶 "View all" 列表页（只需 1 次请求）
2. 把页面按商品拆开，某个商品所在的 HTML 块里出现 "sold out / out of stock" 标记 → 缺货，否则有货
3. 有货商品数 > THRESHOLD 时发邮件（只在"从不超过 → 超过"的那一刻发一次，避免刷屏）

依赖：pip install requests

邮件配置（环境变量）：
  SMTP_USER   发件邮箱，例如 yourname@gmail.com
  SMTP_PASS   邮箱的"应用专用密码"（Gmail 需要先开两步验证再生成，不是登录密码）
  MAIL_TO     收件邮箱（可以和发件相同）
  SMTP_HOST   可选，默认 smtp.gmail.com
  SMTP_PORT   可选，默认 465 (SSL)

用法：
  python matcha_monitor.py --dry-run     只检查并打印每个商品的状态，不发邮件（建议先跑这个，对照网站核对）
  python matcha_monitor.py --dump        把列表页原始 HTML 存成 list_page.html，方便排查
  python matcha_monitor.py --test-mail   发一封测试邮件，验证邮箱配置
  python matcha_monitor.py --once        检查一次就退出（适合配合 cron / 任务计划程序）
  python matcha_monitor.py               常驻运行，每 INTERVAL 秒检查一次
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# 优先使用 curl_cffi（能模拟真实 Chrome 的网络指纹，绕过 403）；没装就退回普通 requests
try:
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    import requests as http
    IMPERSONATE = {}

# ====== 可调参数 ======
LIST_URL = "https://www.marukyu-koyamaen.co.jp/english/shop/products/catalog/matcha/principal"
THRESHOLD = 3          # 有货种类 > 3 时通知（即 4 种及以上）；想改成"3 种及以上"就设为 2
INTERVAL = 300         # 常驻模式下的检查间隔（秒），建议不要低于 120
STATE_FILE = Path(__file__).with_name("matcha_state.json")
DUMP_FILE = Path(__file__).with_name("list_page.html")
# ======================

if IMPERSONATE:
    # curl_cffi 会自动带上和 Chrome 一致的请求头，这里只补一个语言
    HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
else:
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.marukyu-koyamaen.co.jp/english/shop/",
        "Upgrade-Insecure-Requests": "1",
    }

PRODUCT_RE = re.compile(
    r'https?://www\.marukyu-koyamaen\.co\.jp/english/shop/products/([0-9a-z]*\d[0-9a-z]{6,11})(?=["\'?#/])',
    re.I,
)
# 匹配文字 "SOLD OUT" 或 class 名 soldout / sold-out / out-of-stock 等
SOLD_RE = re.compile(r"sold[\s_\-]*out|out[\s_\-]*of[\s_\-]*stock", re.I)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url, retries=2):
    for i in range(retries + 1):
        try:
            r = http.get(url, headers=HEADERS, timeout=30, **IMPERSONATE)
            r.raise_for_status()
            return r.text
        except Exception:
            if i == retries:
                raise
            time.sleep(2 * (i + 1))


def parse_listing(html):
    """返回 [(code, 名称, 是否有货, 链接)]"""
    items, seen = [], set()
    for m in PRODUCT_RE.finditer(html):
        code = m.group(1).lower()
        if code in seen:
            continue
        seen.add(code)
        pos = m.start()
        # 取这个商品所在的 <li> ... </li> 块
        start = html.rfind("<li", 0, pos)
        end = html.find("</li>", pos)
        if start == -1 or end == -1 or pos - start > 4000 or end - pos > 4000:
            start, end = max(0, pos - 300), pos + 1200   # 兜底
        block = html[start:end]

        t = re.search(r'title="([^"]+)"', block)
        if t:
            name = t.group(1).strip()
        else:
            name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()[:60] or code
        sold_out = bool(SOLD_RE.search(block))
        url = f"https://www.marukyu-koyamaen.co.jp/english/shop/products/{code}"
        items.append((code, name, not sold_out, url))
    return items


def scan():
    html = fetch(LIST_URL)
    items = parse_listing(html)
    if not items:
        raise RuntimeError("列表页没有解析到任何商品，可能网站改版或被拦截")
    return items, html


def send_mail(subject, body):
    user, pwd, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pwd and to):
        raise RuntimeError("请先设置环境变量 SMTP_USER / SMTP_PASS / MAIL_TO")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"above": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def run_once(dry_run=False):
    items, _ = scan()
    in_stock = [(n, u) for _, n, ok, u in items if ok]
    n_sold = len(items) - len(in_stock)
    n = len(in_stock)
    log(f"检查完成：共 {len(items)} 个商品，有货 {n} 个，售罄 {n_sold} 个")

    if dry_run:
        for _, name, ok, url in items:
            print(f"  {'有货' if ok else '缺货'}  {name}  {url}")

    warn = ""
    if n_sold == 0:
        warn = "（提示：本次没有识别到任何售罄标记，如与网站实际不符，说明识别规则需要调整）"
        log("警告：没有识别到任何售罄商品。如果网站上明明有 SOLD OUT，请用 --dump 导出 HTML 排查")

    above = n > THRESHOLD
    state = load_state()
    if above and not state.get("above") and not dry_run:
        lines = [f"- {name}\n  {url}" for name, url in in_stock]
        body = (f"当前有货的抹茶共 {n} 种：\n\n" + "\n".join(lines)
                + f"\n\n列表页：{LIST_URL}\n（每单抹茶限购 5 件，购买需要先登录）\n{warn}")
        send_mail(f"🍵 抹茶上货提醒：{n} 种有货", body)
        log("已发送通知邮件")
    elif above and state.get("above"):
        log("仍处于有货状态，已通知过，不重复发送")
    if not dry_run:
        save_state({"above": above})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    args = ap.parse_args()

    if args.test_mail:
        send_mail("抹茶监测：测试邮件", "邮箱配置正常。")
        log("测试邮件已发送")
        return
    if args.dump:
        DUMP_FILE.write_text(fetch(LIST_URL), encoding="utf-8")
        log(f"已保存到 {DUMP_FILE}")
        return
    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    log(f"开始常驻监测，每 {INTERVAL} 秒一次，阈值：有货 > {THRESHOLD} 种")
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"本轮出错：{e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
