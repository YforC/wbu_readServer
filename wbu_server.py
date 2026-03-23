from flask import Flask, request, jsonify, render_template_string
import threading
import time
import requests
import random
import json
import os
import re
import shutil
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

app = Flask(__name__)
DB_FILE = "accounts.json"
db_lock = threading.Lock()

pending_sms_codes = {}
sms_trigger_flags = {}
recapture_flags = {}

def load_db():
    with db_lock:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                try:
                    return json.load(f)
                except Exception as e:
                    logging.error(f"Failed to parse {DB_FILE}: {e}")
                    return {}
        return {}

def save_db(data):
    with db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

def with_db(fn):
    with db_lock:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except Exception:
                    data = {}
        else:
            data = {}
        result = fn(data)
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return result

def update_account_status(username, status_msg, action_req=None):
    def _update(db):
        if username in db:
            db[username]["status"] = status_msg
            if action_req is not None:
                db[username]["action_required"] = action_req
    with_db(_update)
    logging.info(f"[{username}] Status: {status_msg}")

def get_cached_sms(username):
    db = load_db()
    info = db.get(username, {})
    code = info.get("sms_code")
    ts = info.get("sms_code_time", 0)
    if code and (time.time() - ts) < 86400:
        return code
    return None

def save_sms_cache(username, code):
    def _save(db):
        if username in db:
            db[username]["sms_code"] = code
            db[username]["sms_code_time"] = time.time()
    with_db(_save)

def is_account_active(username):
    db = load_db()
    return username in db and db.get(username, {}).get("active", False)

# --- browser capture: open browser -> login -> grab token/readerID -> close browser ---
def browser_capture(username, password, book_id):
    user_data_dir = f"./browser_data_{username}"
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )
        try:
            page = browser.new_page()
            page.on("console", lambda msg: logging.info(f"[{username}] BROWSER: {msg.text}"))

            # === WebVPN login ===
            update_account_status(username, "Opening browser, logging in...")
            page.goto("https://webvpn.wbu.edu.cn/portal/#!/login")
            page.wait_for_load_state('networkidle')

            if "login" in page.url:
                page.wait_for_selector("input.input-txt", state="visible", timeout=15000)
                page.locator("input.input-txt").first.fill(username, force=True)
                page.locator("input#loginPwd").first.fill(password, force=True)
                page.evaluate('''() => {
                    const btn = document.querySelector("button.button--normal[type='submit']");
                    if(btn) btn.click();
                }''')
                page.wait_for_timeout(3000)

                # === SMS verification ===
                if "login" in page.url:
                    cached_code = get_cached_sms(username)
                    if cached_code:
                        logging.info(f"[{username}] Using cached SMS code (skip sending new SMS)")
                        update_account_status(username, "Using cached SMS code...")
                        sms_code = cached_code
                    else:
                        update_account_status(username, "Awaiting SMS Verification", "SMS")
                        pending_sms_codes[username] = None
                        sms_trigger_flags[username] = False
                        wait_count = 0
                        while pending_sms_codes.get(username) is None:
                            if not is_account_active(username):
                                raise Exception("Aborted.")
                            if sms_trigger_flags.get(username):
                                page.evaluate('''() => {
                                    const link = document.querySelector("a.sms-certification__a");
                                    if(link) link.click();
                                }''')
                                update_account_status(username, "SMS sent, enter code...", "SMS")
                                sms_trigger_flags[username] = False
                            time.sleep(2)
                            wait_count += 2
                            if wait_count > 600:
                                raise Exception("Timeout waiting for SMS code.")
                        sms_code = pending_sms_codes.pop(username)
                        save_sms_cache(username, sms_code)

                    update_account_status(username, "Submitting SMS code...")
                    page.evaluate(f'''async () => {{
                        try {{
                            const resp = await fetch('/por/login_sms1.csp?apiversion=1', {{
                                method: 'POST',
                                headers: {{
                                    'Content-Type': 'application/x-www-form-urlencoded',
                                    'Accept': '*/*',
                                    'Cache-Control': 'no-cache',
                                    'X-Requested-With': 'XMLHttpRequest'
                                }},
                                body: 'apiversion=1&svpn_inputsms={sms_code}'
                            }});
                            const t = await resp.text();
                            console.log("SMS_RESP: " + t);
                            window.location.href = "/portal/#!/service";
                        }} catch(e) {{ console.error("FETCH_ERR: " + e); }}
                    }}''')
                    page.wait_for_timeout(8000)
                    if "login" in page.url:
                        page.goto("https://webvpn.wbu.edu.cn/portal/#!/service")
                        page.wait_for_timeout(3000)
                        if "login" in page.url:
                            if cached_code:
                                logging.warning(f"[{username}] Cached SMS expired, clearing")
                                def _clear(db):
                                    if username in db:
                                        db[username]["sms_code"] = ""
                                        db[username]["sms_code_time"] = 0
                                with_db(_clear)
                            raise Exception("SMS Auth Failed.")

            update_account_status(username, "VPN OK, accessing reading platform...")

            # === CAS login ===
            def handle_cas(page):
                if "authserver/login" not in page.url and "ids-wbu-edu-cn" not in page.url:
                    return True
                update_account_status(username, "CAS login...")
                try:
                    page.wait_for_load_state('networkidle')
                    page.wait_for_timeout(2000)
                    page.evaluate(f'''() => {{
                        for (const inp of document.querySelectorAll('input[name="username"]')) {{
                            if (inp.type !== 'hidden' && inp.offsetParent !== null) {{
                                inp.value = '{username}';
                                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                                break;
                            }}
                        }}
                        for (const inp of document.querySelectorAll('input[name="password"], input[type="password"]')) {{
                            if (inp.type !== 'hidden' && inp.offsetParent !== null) {{
                                inp.value = '{password}';
                                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                                break;
                            }}
                        }}
                    }}''')
                    page.wait_for_timeout(500)
                    page.evaluate('''() => {
                        const sels = ['button.auth_login_btn','#login_submit','button[type="submit"]',
                            'input[type="submit"]','.login-btn','form button'];
                        for (const s of sels) {
                            const b = document.querySelector(s);
                            if (b && b.offsetParent !== null) { b.click(); return; }
                        }
                        const f = document.querySelector('form');
                        if (f) f.submit();
                    }''')
                    page.wait_for_load_state('networkidle')
                    page.wait_for_timeout(5000)
                    return "authserver/login" not in page.url
                except Exception as e:
                    logging.warning(f"[{username}] CAS failed: {e}")
                    return False

            # === Capture ReaderID ===
            captured = {"value": None}
            def _on_req(req):
                if "getBookContent" in req.url and "bookReaderId=" in req.url:
                    m = re.search(r'bookReaderId=(\d+)', req.url)
                    if m:
                        captured["value"] = m.group(1)
                        logging.info(f"[{username}] Got bookReaderId: {m.group(1)}")
            page.on("request", _on_req)

            book_url = f"http://ydpj-wbu-edu-cn-8008-p.webvpn.wbu.edu.cn:8118/#/page/book/read/{book_id}"
            page.goto(book_url)
            page.wait_for_load_state('networkidle')
            page.wait_for_timeout(3000)

            handle_cas(page)

            if "authserver/login" not in page.url and "book/read" not in page.url:
                page.goto(book_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(8000)
                handle_cas(page)

            page.wait_for_timeout(5000)
            if not captured["value"] and "book/read" not in page.url:
                page.goto(book_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(8000)

            page.remove_listener("request", _on_req)

            if not captured["value"]:
                logging.error(f"[{username}] Final URL: {page.url}")
                snippet = page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : ''")
                logging.error(f"[{username}] Page: {snippet}")
                raise Exception("ReaderID capture failed.")

            reader_id = captured["value"]
            cookies = browser.cookies()
            token = next((c['value'] for c in cookies if c['name'] == 'yuedu_token'), None)
            twfid = next((c['value'] for c in cookies if c['name'] == 'TWFID'), None)

            if not token:
                raise Exception("Token missing.")

            logging.info(f"[{username}] Capture done: readerID={reader_id}, token={token[:8]}..., twfid={twfid[:8]}...")
            return token, twfid, reader_id

        finally:
            browser.close()
            logging.info(f"[{username}] Browser closed (capture done).")

# --- Heartbeat loop: pure HTTP, no browser needed ---
def heartbeat_loop(username, book_id, token, twfid, reader_id):
    # Check recapture flag at start (may have been set during browser_capture)
    if recapture_flags.get(username):
        recapture_flags[username] = False
        return "recapture"

    start_time = time.time()
    last_tick = time.time()
    loop_duration = 9000
    url_reading = "http://ydpj-wbu-edu-cn-8008-p.webvpn.wbu.edu.cn:8118/server/book/reading"
    headers = {
        "Host": "ydpj-wbu-edu-cn-8008-p.webvpn.wbu.edu.cn:8118",
        "Accept": "application/json, text/plain, */*",
        "Qd-Authorization": f"Bearer {token}",
        "Cookie": f"TWFID={twfid}; yuedu_token={token}",
        "X-Requested-With": "XMLHttpRequest"
    }

    consecutive_failures = 0
    max_failures = 5
    while time.time() - start_time < loop_duration:
        if not is_account_active(username):
            break
        # Check recapture flag each iteration
        if recapture_flags.get(username):
            recapture_flags[username] = False
            logging.info(f"[{username}] Recapture requested, exiting heartbeat")
            return "recapture"

        speed = random.randint(1200, 1500)
        try:
            res = requests.post(url_reading, params={"sf_request_type": "ajax"},
                headers=headers,
                data={"bookReaderId": reader_id, "bookId": book_id, "readSpeed": str(speed)},
                timeout=20)
            if "<html>" in res.text:
                logging.warning(f"[{username}] Session expired.")
                return "expired"
            if res.status_code != 200:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
                now_t = time.time()
                delta = now_t - last_tick
                last_tick = now_t
                elapsed = int((now_t - start_time) / 60)
                def _beat(db, _delta=delta, _elapsed=elapsed, _speed=speed):
                    if username in db:
                        db[username]["last_beat"] = time.strftime("%H:%M:%S")
                        db[username]["total_seconds"] = db[username].get("total_seconds", 0) + _delta
                        db[username]["status"] = f"Running ({_elapsed}min, Speed: {_speed})"
                with_db(_beat)
        except Exception as e:
            consecutive_failures += 1
            logging.warning(f"[{username}] Heartbeat fail ({consecutive_failures}/{max_failures}): {e}")

        if consecutive_failures >= max_failures:
            logging.warning(f"[{username}] Too many failures.")
            return "failed"
        time.sleep(random.randint(60, 90))

    elapsed_total = int((time.time() - start_time) / 60)
    logging.info(f"[{username}] Heartbeat ended after {elapsed_total}min")
    return "timeout"

# --- Worker main loop: capture -> close browser -> heartbeat -> loop ---
def playwright_worker(username):
    while True:
        if not is_account_active(username):
            break
        # Re-read credentials from DB each iteration (book_id may have changed)
        db = load_db()
        info = db.get(username, {})
        password = info.get("password", "")
        book_id = info.get("book_id", "")
        if not password or not book_id:
            update_account_status(username, "Error: missing credentials")
            time.sleep(10)
            continue
        try:
            # Phase 1: open browser and capture
            token, twfid, reader_id = browser_capture(username, password, book_id)
            # Save captured info to DB for display
            def _save_capture(db):
                if username in db:
                    db[username]["token_preview"] = token[:20] + "..."
                    db[username]["twfid_preview"] = (twfid[:20] + "...") if twfid else ""
                    db[username]["reader_id"] = reader_id
            with_db(_save_capture)
            update_account_status(username, f"Heartbeating (RID: {reader_id})")

            # Phase 2: pure HTTP heartbeat (browser is closed)
            result = heartbeat_loop(username, book_id, token, twfid, reader_id)
            logging.info(f"[{username}] Heartbeat result: {result}, will re-capture...")
            if result == "recapture":
                update_account_status(username, "Recapturing tokens...")
            else:
                update_account_status(username, "Session ended, re-authenticating...")
            time.sleep(5)

        except Exception as e:
            update_account_status(username, f"Error: {str(e)[:80]}")
            logging.error(f"[{username}] Worker error: {e}")
            time.sleep(30)

    # Cleanup
    user_data_dir = f"./browser_data_{username}"
    try:
        if os.path.exists(user_data_dir):
            shutil.rmtree(user_data_dir)
            logging.info(f"[{username}] Cleaned up {user_data_dir}")
    except Exception as e:
        logging.warning(f"[{username}] Cleanup failed: {e}")

# --- Manager thread ---
def manager_thread():
    active_threads = {}
    while True:
        try:
            db = load_db()
            for u, info in db.items():
                if info.get("active") and info.get("password") and info.get("book_id"):
                    if u not in active_threads or not active_threads[u].is_alive():
                        t = threading.Thread(target=playwright_worker, args=(u,), daemon=True)
                        t.start()
                        active_threads[u] = t
        except Exception as e:
            logging.error(f"Manager error: {e}")
        time.sleep(10)

# --- Flask UI ---
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>WBU Reading Center</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f6f8; color: #1a1a2e; min-height: 100vh;
        }
        .topbar {
            background: #fff; border-bottom: 1px solid #e2e5e9;
            padding: 16px 24px; display: flex; justify-content: space-between; align-items: center;
        }
        .topbar h1 { font-size: 18px; font-weight: 700; color: #1a1a2e; letter-spacing: -0.3px; }
        .topbar .meta { font-size: 12px; color: #8b95a5; }
        .main { max-width: 1100px; margin: 24px auto; padding: 0 20px; }

        .card {
            background: #fff; border: 1px solid #e2e5e9; border-radius: 8px;
            padding: 20px; margin-bottom: 16px;
        }
        .card-header {
            font-size: 13px; font-weight: 600; color: #8b95a5;
            text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 14px;
        }

        /* Add form */
        .add-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .add-row input {
            flex: 1; min-width: 100px; padding: 9px 12px; border: 1px solid #d1d5db;
            border-radius: 6px; font-size: 13px; outline: none; transition: border 0.15s;
        }
        .add-row input:focus { border-color: #3b82f6; box-shadow: 0 0 0 2px rgba(59,130,246,0.15); }
        .add-row input.w-sm { flex: 0; width: 90px; }

        .btn {
            padding: 9px 16px; border-radius: 6px; cursor: pointer; border: none;
            font-weight: 600; font-size: 13px; transition: all 0.15s; white-space: nowrap;
        }
        .btn-primary { background: #2563eb; color: #fff; }
        .btn-primary:hover { background: #1d4ed8; }
        .btn-danger { background: #fff; color: #dc2626; border: 1px solid #fca5a5; }
        .btn-danger:hover { background: #fef2f2; }
        .btn-sm { padding: 5px 12px; font-size: 12px; }
        .btn-outline { background: #fff; color: #374151; border: 1px solid #d1d5db; }
        .btn-outline:hover { background: #f9fafb; border-color: #9ca3af; }
        .btn-orange { background: #ea580c; color: #fff; }
        .btn-orange:hover { background: #c2410c; }
        .btn-green { background: #16a34a; color: #fff; }
        .btn-green:hover { background: #15803d; }
        .btn-blue-outline { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
        .btn-blue-outline:hover { background: #eff6ff; }
        .btn-red-outline { background: #fff; color: #dc2626; border: 1px solid #fca5a5; }
        .btn-red-outline:hover { background: #fef2f2; }

        /* SMS alert */
        .sms-card {
            background: #fffbeb; border: 1px solid #fbbf24; border-radius: 8px;
            padding: 16px 20px; margin-bottom: 16px;
            display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
        }
        .sms-label { flex: 1; min-width: 200px; }
        .sms-label strong { display: block; font-size: 14px; color: #92400e; margin-bottom: 2px; }
        .sms-label span { font-size: 12px; color: #a16207; }
        .sms-ops { display: flex; gap: 6px; align-items: center; }
        .sms-ops input[type="text"] {
            width: 100px; padding: 7px 10px; border: 1px solid #d97706; border-radius: 6px;
            font-size: 14px; text-align: center; outline: none; font-weight: 600;
            letter-spacing: 2px; background: #fff;
        }
        .sms-ops input[type="text"]:focus { box-shadow: 0 0 0 2px rgba(217,119,6,0.2); }

        /* Table */
        .tbl { width: 100%; border-collapse: collapse; }
        .tbl th {
            text-align: left; padding: 8px 12px; font-size: 11px; font-weight: 600;
            color: #8b95a5; text-transform: uppercase; letter-spacing: 0.3px;
            border-bottom: 1px solid #e2e5e9;
        }
        .tbl td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; font-size: 13px; vertical-align: top; }
        .tbl tr:last-child td { border-bottom: none; }
        .tbl tr:hover td { background: #f9fafb; }

        .badge {
            display: inline-flex; align-items: center; gap: 5px;
            padding: 3px 10px; border-radius: 4px; font-size: 12px; font-weight: 500;
        }
        .badge-ok { background: #ecfdf5; color: #065f46; }
        .badge-err { background: #fef2f2; color: #991b1b; }
        .badge-wait { background: #fffbeb; color: #92400e; }
        .badge-stop { background: #f3f4f6; color: #6b7280; }
        .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
        .dot-g { background: #10b981; }
        .dot-r { background: #ef4444; }
        .dot-y { background: #f59e0b; }
        .dot-gray { background: #9ca3af; }
        .sms-tag {
            font-size: 11px; color: #6b7280; background: #f3f4f6;
            padding: 2px 8px; border-radius: 4px;
        }
        .empty { text-align: center; padding: 32px; color: #9ca3af; font-size: 14px; }

        .capture-info {
            font-size: 10px; color: #9ca3af; margin-top: 4px;
            font-family: "SF Mono", "Fira Code", monospace;
            word-break: break-all;
        }
        .capture-info span { color: #6b7280; }

        /* Inline book edit */
        .book-display { cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }
        .book-display:hover { color: #2563eb; }
        .book-display .edit-icon { font-size: 10px; color: #93c5fd; }
        .book-edit-form {
            display: none; align-items: center; gap: 4px;
        }
        .book-edit-form input[type="text"] {
            width: 90px; padding: 4px 8px; font-size: 12px;
            border: 1px solid #93c5fd; border-radius: 4px; outline: none;
        }
        .book-edit-form input[type="text"]:focus {
            box-shadow: 0 0 0 2px rgba(59,130,246,0.15);
        }
        .book-edit-form .save-btn {
            padding: 3px 8px; font-size: 11px; cursor: pointer;
            background: #2563eb; color: #fff; border: none; border-radius: 4px; font-weight: 600;
        }
        .book-edit-form .cancel-btn {
            padding: 3px 6px; font-size: 13px; cursor: pointer;
            color: #9ca3af; background: none; border: none; line-height: 1;
        }
        .book-edit-form .cancel-btn:hover { color: #6b7280; }

        .actions { display: flex; gap: 4px; justify-content: flex-end; flex-wrap: wrap; }

        @media (max-width: 640px) {
            .add-row { flex-direction: column; }
            .add-row input, .add-row input.w-sm { width: 100%; flex: auto; }
            .sms-card { flex-direction: column; align-items: flex-start; }
            .actions { justify-content: flex-start; }
        }
    </style>
</head>
<body>
    <div class="topbar">
        <h1>WBU Reading Center</h1>
        <span class="meta">Auto-refresh 15s &nbsp; <a href="/" style="color:#3b82f6; text-decoration:none;">Reload</a></span>
    </div>
    <div class="main">
        <div class="card">
            <div class="card-header">Add Account</div>
            <form action="/add" method="POST" class="add-row">
                <input type="text" name="u" placeholder="Student ID" required>
                <input type="password" name="p" placeholder="Password" required>
                <input type="text" name="b" placeholder="Book ID" required class="w-sm">
                <button type="submit" class="btn btn-primary">Add</button>
            </form>
        </div>

        {% for n, i in db.items() %}
            {% if i.action_required == "SMS" %}
            <div class="sms-card">
                <div class="sms-label">
                    <strong>{{ n }} - SMS Verification</strong>
                    <span>Send SMS first, then enter the code. Cached for 24h.</span>
                </div>
                <div class="sms-ops">
                    <form action="/trigger" method="POST" style="display:contents;">
                        <input type="hidden" name="u" value="{{ n }}">
                        <button class="btn btn-sm btn-outline">Send</button>
                    </form>
                    <form action="/submit" method="POST" style="display:contents;">
                        <input type="hidden" name="u" value="{{ n }}">
                        <input type="text" name="c" placeholder="Code" required maxlength="6" inputmode="numeric">
                        <button class="btn btn-sm btn-orange">Submit</button>
                    </form>
                </div>
            </div>
            {% endif %}
        {% endfor %}

        <div class="card">
            <div class="card-header">Accounts</div>
            {% if db %}
            <table class="tbl">
                <thead><tr>
                    <th>ID</th>
                    <th>Book ID</th>
                    <th>Status</th>
                    <th>Last Beat</th>
                    <th>Total</th>
                    <th>SMS</th>
                    <th style="text-align:right;">Actions</th>
                </tr></thead>
                <tbody>
                {% for n, i in db.items() %}
                    {% set is_err = 'Error' in i.status or 'Failed' in i.status %}
                    {% set is_run = 'Running' in i.status or 'Heartbeat' in i.status %}
                    {% set is_stop = not i.active %}
                    <tr>
                        <td style="font-weight:600;">{{ n }}</td>
                        <td>
                            <div class="book-display" id="bd-{{ n }}" onclick="toggleEdit('{{ n }}')">
                                {{ i.book_id }}
                                <span class="edit-icon">&#9998;</span>
                            </div>
                            <form class="book-edit-form" id="be-{{ n }}" action="/update_book" method="POST">
                                <input type="hidden" name="u" value="{{ n }}">
                                <input type="text" name="b" value="{{ i.book_id }}">
                                <button type="submit" class="save-btn">OK</button>
                                <button type="button" class="cancel-btn" onclick="toggleEdit('{{ n }}')">&times;</button>
                            </form>
                        </td>
                        <td>
                            <span class="badge {{ 'badge-stop' if is_stop else ('badge-ok' if is_run else ('badge-err' if is_err else 'badge-wait')) }}">
                                <span class="dot {{ 'dot-gray' if is_stop else ('dot-g' if is_run else ('dot-r' if is_err else 'dot-y')) }}"></span>
                                {{ i.status }}
                            </span>
                            {% if i.get('reader_id') %}
                            <div class="capture-info">
                                <span>RID:</span> {{ i.reader_id }}
                                {% if i.get('token_preview') %}&nbsp; <span>Token:</span> {{ i.token_preview }}{% endif %}
                            </div>
                            {% endif %}
                        </td>
                        <td style="color:#6b7280; font-size:12px;">{{ i.last_beat|default('--') }}</td>
                        <td style="font-size:12px; font-weight:600; color:#374151;">
                            {% set ts = i.get('total_seconds', 0)|int %}
                            {% if ts >= 3600 %}
                                {% set th = ts // 3600 %}
                                {% set tm = (ts % 3600) // 60 %}
                                {{ th }}h {{ '%02d' | format(tm) }}m
                            {% elif ts >= 60 %}
                                {{ ts // 60 }}m
                            {% else %}
                                --
                            {% endif %}
                        </td>
                        <td>
                            {% if i.sms_code and (now - i.get('sms_code_time', 0)) < 86400 %}
                                {% set hrs = ((86400 - (now - i.get('sms_code_time', 0))) / 3600) | round(1) %}
                                <span class="sms-tag">{{ hrs }}h</span>
                            {% else %}
                                <span style="color:#d1d5db;">--</span>
                            {% endif %}
                        </td>
                        <td>
                            <div class="actions">
                                {% if i.active %}
                                <button class="btn btn-sm btn-red-outline" onclick="postAction('/stop','{{ n }}')">Stop</button>
                                <button class="btn btn-sm btn-blue-outline" onclick="postAction('/recapture','{{ n }}')">Recapture</button>
                                {% else %}
                                <button class="btn btn-sm btn-green" onclick="postAction('/start','{{ n }}')">Start</button>
                                {% endif %}
                                <button class="btn btn-sm btn-danger" onclick="if(confirm('Delete {{ n }}?')) postAction('/del','{{ n }}')">Delete</button>
                            </div>
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty">No accounts added yet.</div>
            {% endif %}
        </div>
    </div>
    <script>
        function postAction(url, username) {
            fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'u=' + encodeURIComponent(username)
            }).then(function() { location.reload(); });
        }

        function toggleEdit(u) {
            var d = document.getElementById('bd-' + u);
            var e = document.getElementById('be-' + u);
            if (e.style.display === 'none' || e.style.display === '') {
                d.style.display = 'none';
                e.style.display = 'flex';
                var inp = e.querySelector('input[name="b"]');
                inp.focus();
                inp.select();
            } else {
                d.style.display = '';
                e.style.display = 'none';
            }
        }

        setTimeout(function() { location.reload(); }, 15000);
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    db = load_db()
    return render_template_string(HTML_TEMPLATE, db=db, now=time.time())

@app.route('/add', methods=['POST'])
def add():
    u, p, b = request.form.get('u'), request.form.get('p'), request.form.get('b')
    if u and p and b:
        def _add(db):
            db[u] = {
                "password": p, "book_id": b, "status": "Pending...",
                "active": True, "action_required": "",
                "sms_code": "", "sms_code_time": 0,
                "total_seconds": 0
            }
        with_db(_add)
    return "<script>window.location.href='/';</script>"

@app.route('/trigger', methods=['POST'])
def trigger():
    u = request.form.get('u')
    if u:
        sms_trigger_flags[u] = True
    return "<script>window.location.href='/';</script>"

@app.route('/submit', methods=['POST'])
def submit():
    u, c = request.form.get('u'), request.form.get('c')
    if u and c:
        pending_sms_codes[u] = c
        def _update(db):
            if u in db:
                db[u]["action_required"] = ""
                db[u]["sms_code"] = c
                db[u]["sms_code_time"] = time.time()
        with_db(_update)
    return "<script>window.location.href='/';</script>"

@app.route('/del', methods=['POST'])
def delete():
    u = request.form.get('u')
    def _del(db):
        if u in db:
            del db[u]
    with_db(_del)
    recapture_flags.pop(u, None)
    return "<script>window.location.href='/';</script>"

@app.route('/update_book', methods=['POST'])
def update_book():
    u = request.form.get('u')
    b = request.form.get('b')
    if u and b:
        def _update(db):
            if u in db:
                db[u]["book_id"] = b
        with_db(_update)
        # Trigger recapture with new book_id
        if is_account_active(u):
            recapture_flags[u] = True
            update_account_status(u, f"Book changed to {b}, recapturing...")
    return "<script>window.location.href='/';</script>"

@app.route('/stop', methods=['POST'])
def stop_account():
    u = request.form.get('u')
    if u:
        recapture_flags.pop(u, None)
        def _stop(db):
            if u in db:
                db[u]["active"] = False
                db[u]["status"] = "Stopped"
                db[u]["action_required"] = ""
        with_db(_stop)
        logging.info(f"[{u}] Manually stopped")
    return "<script>window.location.href='/';</script>"

@app.route('/start', methods=['POST'])
def start_account():
    u = request.form.get('u')
    if u:
        def _start(db):
            if u in db:
                db[u]["active"] = True
                db[u]["status"] = "Starting..."
        with_db(_start)
        logging.info(f"[{u}] Manually started")
    return "<script>window.location.href='/';</script>"

@app.route('/recapture', methods=['POST'])
def recapture():
    u = request.form.get('u')
    if u:
        recapture_flags[u] = True
        update_account_status(u, "Recapture requested...")
        logging.info(f"[{u}] Recapture requested via panel")
    return "<script>window.location.href='/';</script>"

if __name__ == "__main__":
    threading.Thread(target=manager_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
