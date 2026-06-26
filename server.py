#!/usr/bin/env python3
import sqlite3
import json
import urllib.parse
import os
import re
import time
import ssl
import hashlib
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler

IMAGES_DIR = 'images'
START_TIME = str(int(time.time()))
PRODUCTS_DB = 'products.db'
SHEET_ID = '1xjDKlkehpL1sOqwJExLOdOibKhpnYi9fOra5XIBqk14'
PG_URL = os.environ.get('PRODUCTS_DB', '')  # PRODUCTS_DB 변수 재활용
VAPID_PUBLIC_KEY  = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_EMAIL       = os.environ.get('VAPID_EMAIL', 'mailto:admin@example.com')

NEON_HOST = 'ep-muddy-firefly-atf2x9hz.c-9.us-east-1.aws.neon.tech'
NEON_DB   = 'neondb'
NEON_USER = 'neondb_owner'
NEON_PASS = 'npg_FudX2Rp4iYOw'

def data_db():
    import pg8000.dbapi as pg
    url = PG_URL if PG_URL.startswith('postgresql') else ''
    if url:
        r = urllib.parse.urlparse(url)
        host, port, database, user, password = r.hostname, r.port or 5432, r.path[1:], r.username, r.password
    else:
        host, port, database, user, password = NEON_HOST, 5432, NEON_DB, NEON_USER, NEON_PASS
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg.connect(host=host, port=port, database=database, user=user, password=password, ssl_context=ctx)

def rows_to_dicts(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]

def init_tables():
    conn = data_db(); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS comments (
        id SERIAL PRIMARY KEY,
        barcode TEXT, content TEXT, created_at TEXT, parent_id INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id SERIAL PRIMARY KEY,
        endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS issues (
        id SERIAL PRIMARY KEY,
        title TEXT, occurred_at TEXT DEFAULT '', ended_at TEXT DEFAULT '',
        content TEXT DEFAULT '', status TEXT DEFAULT '진행중', created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        barcode TEXT, name TEXT, qty INTEGER DEFAULT 1,
        order_date TEXT DEFAULT '', payment TEXT DEFAULT '미불',
        ordered TEXT DEFAULT '미완료', pickup_date TEXT DEFAULT '',
        customer TEXT DEFAULT '', phone TEXT DEFAULT '',
        delivery TEXT DEFAULT '없음', address TEXT DEFAULT '',
        staff TEXT DEFAULT '', note TEXT DEFAULT '',
        created_at TEXT DEFAULT '', completed INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS as_requests (
        id SERIAL PRIMARY KEY,
        received_date TEXT DEFAULT '', product_name TEXT DEFAULT '',
        content TEXT DEFAULT '', customer TEXT DEFAULT '', phone TEXT DEFAULT '',
        delivery TEXT DEFAULT '없음', staff TEXT DEFAULT '',
        note TEXT DEFAULT '', status TEXT DEFAULT '진행중', created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS suggestions (
        id SERIAL PRIMARY KEY,
        content TEXT DEFAULT '', date TEXT DEFAULT '',
        status TEXT DEFAULT '미처리', created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires_at BIGINT NOT NULL
    )''')
    conn.commit(); conn.close()

def search_products(query='', barcode='', limit=50, offset=0):
    conn = sqlite3.connect(PRODUCTS_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if barcode:
        c.execute('SELECT * FROM products WHERE barcode=? LIMIT 1', (barcode,))
        rows = c.fetchall(); total = len(rows)
    elif query:
        like = f'%{query.lower().replace(" ","")}%'
        w = "replace(lower({c}), ' ', '') LIKE ?"
        where = f"{w.format(c='name')} OR {w.format(c='barcode')} OR {w.format(c='author')}"
        c.execute(f'SELECT COUNT(*) FROM products WHERE {where}', (like,like,like))
        total = c.fetchone()[0]
        c.execute(f'SELECT * FROM products WHERE {where} LIMIT ? OFFSET ?', (like,like,like,limit,offset))
        rows = c.fetchall()
    else:
        c.execute('SELECT COUNT(*) FROM products'); total = c.fetchone()[0]
        c.execute('SELECT * FROM products LIMIT ? OFFSET ?', (limit,offset)); rows = c.fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result, total

def get_comments(barcode):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT id,content,created_at,parent_id FROM comments WHERE barcode=%s ORDER BY id', (barcode,))
    rows = rows_to_dicts(c); conn.close()
    top = [r for r in rows if not r['parent_id']]
    replies = {}
    for r in rows:
        if r['parent_id']: replies.setdefault(r['parent_id'], []).append(r)
    for t in top: t['replies'] = replies.get(t['id'], [])
    return top

def add_comment(barcode, content, created_at, parent_id=None):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO comments(barcode,content,created_at,parent_id) VALUES(%s,%s,%s,%s) RETURNING id',
              (barcode, content, created_at, parent_id))
    new_id = c.fetchone()[0]; conn.commit(); conn.close()
    return new_id

def delete_comment(comment_id):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM comments WHERE id=%s', (comment_id,))
    conn.commit(); conn.close()

def search_comments(query):
    pconn = sqlite3.connect(PRODUCTS_DB); pconn.row_factory = sqlite3.Row
    products = {r['barcode']: dict(r) for r in pconn.execute('SELECT barcode,name,author,publisher,price FROM products')}
    pconn.close()
    conn = data_db(); c = conn.cursor()
    like = f"%{query.lower().replace(' ','')}%"
    c.execute('''SELECT id,barcode,content,created_at FROM comments
                 WHERE replace(lower(content),' ','') LIKE %s ORDER BY id DESC LIMIT 100''', (like,))
    rows = rows_to_dicts(c); conn.close()
    for r in rows:
        p = products.get(r['barcode'], {})
        r.update({'name': p.get('name',''), 'author': p.get('author',''),
                  'publisher': p.get('publisher',''), 'price': p.get('price',0)})
    return rows

def save_subscription(endpoint, p256dh, auth):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO push_subscriptions(endpoint,p256dh,auth) VALUES(%s,%s,%s)
                 ON CONFLICT(endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth''',
              (endpoint, p256dh, auth))
    conn.commit(); conn.close()

def get_subscriptions():
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM push_subscriptions')
    rows = rows_to_dicts(c); conn.close(); return rows

def send_push_notification(title, body):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        print('VAPID 키 없음'); return
    try:
        from pywebpush import webpush, WebPushException
        subs = get_subscriptions()
        print(f'푸시 발송: {len(subs)}명')
        for sub in subs:
            try:
                webpush(subscription_info={'endpoint': sub['endpoint'],
                                           'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}},
                        data=json.dumps({'title': title, 'body': body}, ensure_ascii=False),
                        vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={'sub': VAPID_EMAIL})
            except WebPushException as e:
                print(f'푸시 실패: {e}')
    except Exception as e:
        print(f'푸시 오류: {e}')

def get_orders(barcode=''):
    conn = data_db(); c = conn.cursor()
    if barcode: c.execute('SELECT * FROM orders WHERE barcode=%s ORDER BY id DESC', (barcode,))
    else: c.execute('SELECT * FROM orders ORDER BY id DESC')
    rows = rows_to_dicts(c); conn.close(); return rows

def add_order(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO orders(barcode,name,qty,order_date,payment,ordered,pickup_date,
                 customer,phone,delivery,address,staff,note,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
              (data.get('barcode',''), data.get('name',''), data.get('qty',1),
               data.get('order_date',''), data.get('payment','미불'), data.get('ordered','미완료'),
               data.get('pickup_date',''), data.get('customer',''), data.get('phone',''),
               data.get('delivery','없음'), data.get('address',''),
               data.get('staff',''), data.get('note',''), data.get('created_at','')))
    new_id = c.fetchone()[0]; conn.commit(); conn.close(); return new_id

def update_order(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''UPDATE orders SET qty=%s,order_date=%s,payment=%s,ordered=%s,pickup_date=%s,
                 customer=%s,phone=%s,delivery=%s,address=%s,staff=%s,note=%s WHERE id=%s''',
              (data.get('qty',1), data.get('order_date',''), data.get('payment','미불'),
               data.get('ordered','미완료'), data.get('pickup_date',''), data.get('customer',''),
               data.get('phone',''), data.get('delivery','없음'), data.get('address',''),
               data.get('staff',''), data.get('note',''), data['id']))
    conn.commit(); conn.close()

def delete_order(order_id):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM orders WHERE id=%s', (order_id,))
    conn.commit(); conn.close()

def get_issues():
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM issues ORDER BY id DESC')
    rows = rows_to_dicts(c); conn.close(); return rows

def add_issue(data):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO issues(title,occurred_at,ended_at,content,status,created_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',
              (data.get('title',''), data.get('occurred_at',''), data.get('ended_at',''),
               data.get('content',''), '진행중', data.get('created_at','')))
    new_id = c.fetchone()[0]; conn.commit(); conn.close(); return new_id

def update_issue(data):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE issues SET title=%s,occurred_at=%s,ended_at=%s,content=%s WHERE id=%s',
              (data.get('title',''), data.get('occurred_at',''), data.get('ended_at',''),
               data.get('content',''), data['id']))
    conn.commit(); conn.close()

def set_issue_status(issue_id, status):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE issues SET status=%s WHERE id=%s', (status, issue_id))
    conn.commit(); conn.close()

def delete_issue(issue_id):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM issues WHERE id=%s', (issue_id,))
    conn.commit(); conn.close()

def get_as_requests():
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM as_requests ORDER BY id DESC')
    rows = rows_to_dicts(c); conn.close(); return rows

def add_as_request(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO as_requests(received_date,product_name,content,customer,phone,delivery,staff,note,status,created_at)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
              (data.get('received_date',''), data.get('product_name',''), data.get('content',''),
               data.get('customer',''), data.get('phone',''), data.get('delivery','없음'),
               data.get('staff',''), data.get('note',''), '진행중', data.get('created_at','')))
    new_id = c.fetchone()[0]; conn.commit(); conn.close(); return new_id

def update_as_request(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''UPDATE as_requests SET received_date=%s,product_name=%s,content=%s,customer=%s,
                 phone=%s,delivery=%s,staff=%s,note=%s WHERE id=%s''',
              (data.get('received_date',''), data.get('product_name',''), data.get('content',''),
               data.get('customer',''), data.get('phone',''), data.get('delivery','없음'),
               data.get('staff',''), data.get('note',''), data['id']))
    conn.commit(); conn.close()

def set_as_status(as_id, status):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE as_requests SET status=%s WHERE id=%s', (status, as_id))
    conn.commit(); conn.close()

def delete_as_request(as_id):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM as_requests WHERE id=%s', (as_id,))
    conn.commit(); conn.close()

def get_suggestions():
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM suggestions ORDER BY id DESC')
    rows = rows_to_dicts(c); conn.close(); return rows

def add_suggestion(data):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO suggestions(content,date,status,created_at) VALUES(%s,%s,%s,%s) RETURNING id',
              (data.get('content',''), data.get('date',''), '미처리', data.get('created_at','')))
    new_id = c.fetchone()[0]; conn.commit(); conn.close(); return new_id

def set_suggestion_status(sid, status):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE suggestions SET status=%s WHERE id=%s', (status, sid))
    conn.commit(); conn.close()

def delete_suggestion(sid):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM suggestions WHERE id=%s', (sid,))
    conn.commit(); conn.close()

def toggle_complete(order_id):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE orders SET completed=1-completed WHERE id=%s', (order_id,))
    conn.commit(); conn.close()

def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex()
    return f'{salt}${h}'

def verify_password(pw, stored):
    try:
        salt, h = stored.split('$')
        return hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex() == h
    except: return False

def register_user(username, password, display_name):
    conn = data_db(); c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password_hash, display_name, created_at) VALUES (%s,%s,%s,%s) RETURNING id',
                  (username.strip(), hash_password(password), display_name.strip(), str(int(time.time()))))
        user_id = c.fetchone()[0]
        conn.commit()
        return {'id': user_id, 'username': username, 'display_name': display_name}
    except Exception as e:
        conn.rollback()
        raise e
    finally: conn.close()

def login_user(username, password):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT id, username, display_name, password_hash FROM users WHERE username=%s', (username.strip(),))
    rows = rows_to_dicts(c); conn.close()
    if not rows: return None
    u = rows[0]
    if not verify_password(password, u['password_hash']): return None
    return {'id': u['id'], 'username': u['username'], 'display_name': u['display_name']}

def create_session(user_id):
    token = secrets.token_hex(32)
    expires = int(time.time()) + 30 * 24 * 3600  # 30일
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO sessions (token, user_id, expires_at) VALUES (%s,%s,%s)', (token, user_id, expires))
    conn.commit(); conn.close()
    return token

def verify_session(token):
    if not token: return None
    conn = data_db(); c = conn.cursor()
    c.execute('''SELECT u.id, u.username, u.display_name FROM sessions s
                 JOIN users u ON u.id = s.user_id
                 WHERE s.token=%s AND s.expires_at > %s''', (token, int(time.time())))
    rows = rows_to_dicts(c); conn.close()
    return rows[0] if rows else None

def delete_session(token):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM sessions WHERE token=%s', (token,))
    conn.commit(); conn.close()

def fetch_schedule(year_short, month):
    import urllib.request, csv, io
    sheet_name = f'{year_short}년 {month}월'
    url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}'
    with urllib.request.urlopen(url, timeout=10) as resp:
        csv_text = resp.read().decode('utf-8')
    rows = list(csv.reader(io.StringIO(csv_text)))
    schedule = {}
    i = 0
    while i < len(rows):
        row = rows[i]
        label = row[0].strip() if row else ''
        if not row or not (label == '구분' or label.endswith('구분')):
            i += 1
            continue
        date_row = rows[i+1] if i+1 < len(rows) else []
        dates = []
        for c in range(1, min(15, len(date_row)), 2):
            d = date_row[c].strip()
            if d and '일' in d:
                dates.append((c, d))
        work_row = None
        off_rows = []
        collecting_off = False
        for j in range(i+2, min(i+12, len(rows))):
            r = rows[j]
            if not r: continue
            label = r[0].strip()
            if label == '근무':
                work_row = r
            elif label == '휴무':
                off_rows.append(r); collecting_off = True
            elif label == '' and collecting_off:
                off_rows.append(r)
            elif label == '구분' and j > i+2:
                break
            elif label not in ('', '특이사항', '근무', '휴무'):
                collecting_off = False
        for col, date in dates:
            if date not in schedule:
                schedule[date] = {'오전': '', '오후': '', '휴무': []}
            if work_row:
                am = work_row[col].strip() if col < len(work_row) else ''
                pm = work_row[col+1].strip() if col+1 < len(work_row) else ''
                if am: schedule[date]['오전'] = am
                if pm: schedule[date]['오후'] = pm
            for off_row in off_rows:
                person = off_row[col].strip() if col < len(off_row) else ''
                if person: schedule[date]['휴무'].append(person)
        i += 1
    # 날짜 정렬
    def date_key(d):
        m = re.search(r'(\d+)월\s*(\d+)일', d)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return [{'date': d, **schedule[d]} for d in sorted(schedule, key=date_key)]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == '/api/image':
            barcode = re.sub(r'[^\w\-]', '', params.get('barcode',[''])[0])
            for ext in ['jpg','jpeg','png','webp']:
                path = os.path.join(IMAGES_DIR, f'{barcode}.{ext}')
                if os.path.exists(path):
                    with open(path,'rb') as f: data = f.read()
                    mime = 'image/jpeg' if ext in ('jpg','jpeg') else f'image/{ext}'
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', len(data))
                    self.end_headers(); self.wfile.write(data); return
            self.send_response(404); self.end_headers()
        elif parsed.path == '/api/comments':
            self.send_json(get_comments(params.get('barcode',[''])[0]))
        elif parsed.path == '/api/comments/search':
            self.send_json(search_comments(params.get('q',[''])[0]))
        elif parsed.path == '/api/version':
            self.send_json({'version': START_TIME})
        elif parsed.path == '/api/push/key':
            self.send_json({'publicKey': VAPID_PUBLIC_KEY})
        elif parsed.path == '/api/push/debug':
            self.send_json({'count': len(get_subscriptions()), 'vapid_key_set': bool(VAPID_PUBLIC_KEY)})
        elif parsed.path == '/api/env':
            db_path_val = os.environ.get('DB_PATH', 'NOT_SET')
            self.send_json({'keys': sorted(os.environ.keys()), 'DB_PATH_value': db_path_val[:40], 'PG_URL_module': PG_URL[:40] if PG_URL else 'EMPTY'})
        elif parsed.path == '/api/dbinfo':
            db_url = os.environ.get('DATABASE_URL', '')
            self.send_json({'DATABASE_URL_set': bool(db_url), 'backend': 'postgresql'})
        elif parsed.path == '/api/as':
            self.send_json(get_as_requests())
        elif parsed.path == '/api/suggestions':
            self.send_json(get_suggestions())
        elif parsed.path == '/api/issues':
            self.send_json(get_issues())
        elif parsed.path == '/api/auth/me':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if user: self.send_json({'ok': True, 'user': user})
            else: self.send_json({'ok': False})
        elif parsed.path == '/api/schedule':
            try:
                now = time.localtime()
                year_short = params.get('year', [str(now.tm_year % 100)])[0]
                month = params.get('month', [str(now.tm_mon)])[0]
                self.send_json({'ok': True, 'data': fetch_schedule(year_short, month)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})
        elif parsed.path == '/api/orders':
            self.send_json(get_orders(params.get('barcode',[''])[0]))
        elif parsed.path == '/api/search':
            products, total = search_products(
                params.get('q',[''])[0], params.get('barcode',[''])[0],
                int(params.get('limit',['50'])[0]), int(params.get('offset',['0'])[0]))
            self.send_json({'products': products, 'total': total})
        else:
            self.send_file(parsed.path)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))

        if self.path == '/api/auth/register':
            try:
                username = body.get('username','').strip()
                password = body.get('password','').strip()
                display_name = body.get('display_name','').strip()
                if not username or not password or not display_name:
                    self.send_json({'ok': False, 'error': '모든 항목을 입력해주세요'}); return
                if len(password) < 4:
                    self.send_json({'ok': False, 'error': '비밀번호는 4자 이상이어야 합니다'}); return
                user = register_user(username, password, display_name)
                token = create_session(user['id'])
                self.send_json({'ok': True, 'token': token, 'user': user})
            except Exception as e:
                msg = str(e)
                if 'unique' in msg.lower() or 'duplicate' in msg.lower():
                    self.send_json({'ok': False, 'error': '이미 사용 중인 아이디입니다'})
                else:
                    self.send_json({'ok': False, 'error': '오류가 발생했습니다'})
        elif self.path == '/api/auth/login':
            user = login_user(body.get('username',''), body.get('password',''))
            if not user: self.send_json({'ok': False, 'error': '아이디 또는 비밀번호가 틀렸습니다'}); return
            token = create_session(user['id'])
            self.send_json({'ok': True, 'token': token, 'user': user})
        elif self.path == '/api/auth/logout':
            delete_session(self.headers.get('X-Token',''))
            self.send_json({'ok': True})
        elif self.path == '/api/comments':
            self.send_json({'ok': True, 'id': add_comment(body['barcode'], body['content'], body['created_at'], body.get('parent_id'))})
        elif self.path == '/api/comments/delete':
            delete_comment(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/push/subscribe':
            keys = body.get('keys', {})
            save_subscription(body.get('endpoint',''), keys.get('p256dh',''), keys.get('auth',''))
            print(f'[PUSH] 구독 저장. 총: {len(get_subscriptions())}명')
            self.send_json({'ok': True})
        elif self.path == '/api/orders':
            new_id = add_order(body)
            send_push_notification('새 고객 주문',
                f"{body.get('name','')}{' · '+body.get('customer','') if body.get('customer') else ''}")
            self.send_json({'ok': True, 'id': new_id})
        elif self.path == '/api/orders/update':
            update_order(body); self.send_json({'ok': True})
        elif self.path == '/api/orders/delete':
            delete_order(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/orders/complete':
            toggle_complete(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/as':
            self.send_json({'ok': True, 'id': add_as_request(body)})
        elif self.path == '/api/as/update':
            update_as_request(body); self.send_json({'ok': True})
        elif self.path == '/api/as/status':
            set_as_status(body['id'], body['status']); self.send_json({'ok': True})
        elif self.path == '/api/as/delete':
            delete_as_request(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/suggestions':
            self.send_json({'ok': True, 'id': add_suggestion(body)})
        elif self.path == '/api/suggestions/status':
            set_suggestion_status(body['id'], body['status']); self.send_json({'ok': True})
        elif self.path == '/api/suggestions/delete':
            delete_suggestion(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/issues':
            self.send_json({'ok': True, 'id': add_issue(body)})
        elif self.path == '/api/issues/update':
            update_issue(body); self.send_json({'ok': True})
        elif self.path == '/api/issues/status':
            set_issue_status(body['id'], body['status']); self.send_json({'ok': True})
        elif self.path == '/api/issues/delete':
            delete_issue(body['id']); self.send_json({'ok': True})
        else:
            self.send_response(404); self.end_headers()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers(); self.wfile.write(body)

    def send_file(self, path):
        if path in ('/', ''): path = '/index.html'
        try:
            with open('.' + path, 'rb') as f: content = f.read()
            ext = path.split('.')[-1]
            types = {'html':'text/html','js':'text/javascript','css':'text/css','json':'application/json'}
            self.send_response(200)
            self.send_header('Content-Type', types.get(ext,'application/octet-stream') + '; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.end_headers(); self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def log_message(self, format, *args): pass

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # 상품 DB 없으면 다운로드
    if not os.path.exists(PRODUCTS_DB):
        import urllib.request
        os.makedirs(os.path.dirname(os.path.abspath(PRODUCTS_DB)) or '.', exist_ok=True)
        print('상품 DB 다운로드 중...')
        urllib.request.urlretrieve(
            'https://github.com/eulgilees/sangpum-catalog/releases/download/v1.0/products.db', PRODUCTS_DB)
        print('다운로드 완료!')
    pg_url_val = os.environ.get('PG_URL', 'NOT_SET')
    print(f'=== PG_URL 값 === [{pg_url_val[:30] if pg_url_val != "NOT_SET" else "NOT_SET"}]')
    print('PostgreSQL 테이블 초기화...')
    try:
        init_tables()
        print('PostgreSQL 연결 성공!')
    except Exception as e:
        print(f'DB 초기화 실패 (서버는 계속 실행): {e}')
    port = int(os.environ.get('PORT', 8747))
    print(f'서버 시작: http://localhost:{port}')
    HTTPServer(('', port), Handler).serve_forever()
