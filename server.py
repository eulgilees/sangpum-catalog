#!/usr/bin/env python3
import sqlite3
import json
import urllib.parse
import os
import re
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

IMAGES_DIR = 'images'
START_TIME = str(int(time.time()))
DB_PATH = os.environ.get('DB_PATH', 'products.db')
DATABASE_URL = os.environ.get('DATABASE_URL', '')  # Railway PostgreSQL
VAPID_PUBLIC_KEY  = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_EMAIL       = os.environ.get('VAPID_EMAIL', 'mailto:admin@example.com')

# ── PostgreSQL 연결 ──────────────────────────────────────────────
def pg():
    import psycopg2
    url = DATABASE_URL
    # postgres:// → postgresql:// 변환
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    conn = psycopg2.connect(url, sslmode='require')
    return conn

def pg_row(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

# ── 테이블 초기화 ─────────────────────────────────────────────────
def init_pg_tables():
    conn = pg()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS comments (
        id SERIAL PRIMARY KEY,
        barcode TEXT, content TEXT, created_at TEXT, parent_id INTEGER
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id SERIAL PRIMARY KEY,
        endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        barcode TEXT, name TEXT, qty INTEGER DEFAULT 1,
        order_date TEXT DEFAULT '', payment TEXT DEFAULT '미불',
        ordered TEXT DEFAULT '미완료', pickup_date TEXT DEFAULT '',
        customer TEXT DEFAULT '', phone TEXT DEFAULT '',
        delivery TEXT DEFAULT '없음', address TEXT DEFAULT '',
        staff TEXT DEFAULT '', note TEXT DEFAULT '',
        created_at TEXT DEFAULT '', completed INTEGER DEFAULT 0
    )''')
    conn.commit()
    cur.close(); conn.close()

# ── 상품 검색 (SQLite) ────────────────────────────────────────────
def search_products(query='', barcode='', limit=50, offset=0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if barcode:
        c.execute('SELECT * FROM products WHERE barcode=? LIMIT 1', (barcode,))
        rows = c.fetchall()
        total = len(rows)
    elif query:
        normalized = query.lower().replace(' ', '')
        like = f'%{normalized}%'
        norm_col = "replace(lower({col}), ' ', '')"
        where = f"{norm_col.format(col='name')} LIKE ? OR {norm_col.format(col='barcode')} LIKE ? OR {norm_col.format(col='author')} LIKE ?"
        c.execute(f'SELECT COUNT(*) FROM products WHERE {where}', (like, like, like))
        total = c.fetchone()[0]
        c.execute(f'SELECT * FROM products WHERE {where} LIMIT ? OFFSET ?', (like, like, like, limit, offset))
        rows = c.fetchall()
    else:
        c.execute('SELECT COUNT(*) FROM products')
        total = c.fetchone()[0]
        c.execute('SELECT * FROM products LIMIT ? OFFSET ?', (limit, offset))
        rows = c.fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result, total

# ── 댓글 (PostgreSQL) ─────────────────────────────────────────────
def get_comments(barcode):
    conn = pg(); cur = conn.cursor()
    cur.execute('SELECT id, content, created_at, parent_id FROM comments WHERE barcode=%s ORDER BY id ASC', (barcode,))
    rows = pg_row(cur)
    cur.close(); conn.close()
    top = [r for r in rows if not r['parent_id']]
    replies = {}
    for r in rows:
        if r['parent_id']:
            replies.setdefault(r['parent_id'], []).append(r)
    for t in top:
        t['replies'] = replies.get(t['id'], [])
    return top

def add_comment(barcode, content, created_at, parent_id=None):
    conn = pg(); cur = conn.cursor()
    cur.execute('INSERT INTO comments(barcode,content,created_at,parent_id) VALUES(%s,%s,%s,%s) RETURNING id',
                (barcode, content, created_at, parent_id))
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return new_id

def delete_comment(comment_id):
    conn = pg(); cur = conn.cursor()
    cur.execute('DELETE FROM comments WHERE id=%s', (comment_id,))
    conn.commit(); cur.close(); conn.close()

def search_comments(query):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    products = {r['barcode']: dict(r) for r in conn.execute('SELECT barcode,name,author,publisher,price FROM products')}
    conn.close()

    pg_conn = pg(); cur = pg_conn.cursor()
    like = f"%{query.lower().replace(' ', '')}%"
    cur.execute('''SELECT id, barcode, content, created_at FROM comments
                   WHERE replace(lower(content), ' ', '') LIKE %s
                   ORDER BY id DESC LIMIT 100''', (like,))
    rows = pg_row(cur)
    cur.close(); pg_conn.close()

    for r in rows:
        p = products.get(r['barcode'], {})
        r['name'] = p.get('name', '')
        r['author'] = p.get('author', '')
        r['publisher'] = p.get('publisher', '')
        r['price'] = p.get('price', 0)
    return rows

# ── 푸시 구독 (PostgreSQL) ────────────────────────────────────────
def save_subscription(endpoint, p256dh, auth):
    conn = pg(); cur = conn.cursor()
    cur.execute('''INSERT INTO push_subscriptions(endpoint,p256dh,auth) VALUES(%s,%s,%s)
                   ON CONFLICT(endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth''',
                (endpoint, p256dh, auth))
    conn.commit(); cur.close(); conn.close()

def get_subscriptions():
    conn = pg(); cur = conn.cursor()
    cur.execute('SELECT * FROM push_subscriptions')
    rows = pg_row(cur)
    cur.close(); conn.close()
    return rows

def send_push_notification(title, body):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        print('VAPID 키 없음, 푸시 스킵'); return
    try:
        from pywebpush import webpush, WebPushException
        subs = get_subscriptions()
        print(f'푸시 발송: {len(subs)}명, title={title}')
        for sub in subs:
            try:
                webpush(
                    subscription_info={'endpoint': sub['endpoint'],
                                       'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}},
                    data=json.dumps({'title': title, 'body': body}, ensure_ascii=False),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': VAPID_EMAIL}
                )
                print(f'푸시 성공: {sub["endpoint"][:40]}...')
            except WebPushException as e:
                print(f'푸시 실패: {e}')
    except Exception as e:
        print(f'푸시 오류: {e}')

# ── 주문 (PostgreSQL) ─────────────────────────────────────────────
def get_orders(barcode=''):
    conn = pg(); cur = conn.cursor()
    if barcode:
        cur.execute('SELECT * FROM orders WHERE barcode=%s ORDER BY id DESC', (barcode,))
    else:
        cur.execute('SELECT * FROM orders ORDER BY id DESC')
    rows = pg_row(cur)
    cur.close(); conn.close()
    return rows

def add_order(data):
    conn = pg(); cur = conn.cursor()
    cur.execute('''INSERT INTO orders(barcode,name,qty,order_date,payment,ordered,pickup_date,customer,phone,delivery,address,staff,note,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (data.get('barcode',''), data.get('name',''), data.get('qty',1),
                 data.get('order_date',''), data.get('payment','미불'), data.get('ordered','미완료'),
                 data.get('pickup_date',''), data.get('customer',''), data.get('phone',''),
                 data.get('delivery','없음'), data.get('address',''),
                 data.get('staff',''), data.get('note',''), data.get('created_at','')))
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return new_id

def update_order(data):
    conn = pg(); cur = conn.cursor()
    cur.execute('''UPDATE orders SET qty=%s,order_date=%s,payment=%s,ordered=%s,pickup_date=%s,
                   customer=%s,phone=%s,delivery=%s,address=%s,staff=%s,note=%s WHERE id=%s''',
                (data.get('qty',1), data.get('order_date',''), data.get('payment','미불'),
                 data.get('ordered','미완료'), data.get('pickup_date',''), data.get('customer',''),
                 data.get('phone',''), data.get('delivery','없음'), data.get('address',''),
                 data.get('staff',''), data.get('note',''), data['id']))
    conn.commit(); cur.close(); conn.close()

def delete_order(order_id):
    conn = pg(); cur = conn.cursor()
    cur.execute('DELETE FROM orders WHERE id=%s', (order_id,))
    conn.commit(); cur.close(); conn.close()

def toggle_complete(order_id):
    conn = pg(); cur = conn.cursor()
    cur.execute('UPDATE orders SET completed=1-completed WHERE id=%s', (order_id,))
    conn.commit(); cur.close(); conn.close()

# ── HTTP 핸들러 ───────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == '/api/image':
            barcode = params.get('barcode', [''])[0]
            barcode = re.sub(r'[^\w\-]', '', barcode)
            for ext in ['jpg', 'jpeg', 'png', 'webp']:
                path = os.path.join(IMAGES_DIR, f'{barcode}.{ext}')
                if os.path.exists(path):
                    with open(path, 'rb') as f:
                        data = f.read()
                    mime = 'image/jpeg' if ext in ('jpg','jpeg') else f'image/{ext}'
                    self.send_response(200)
                    self.send_header('Content-Type', mime)
                    self.send_header('Content-Length', len(data))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(404); self.end_headers()

        elif parsed.path == '/api/comments':
            barcode = params.get('barcode', [''])[0]
            self.send_json(get_comments(barcode))
        elif parsed.path == '/api/comments/search':
            self.send_json(search_comments(params.get('q', [''])[0]))
        elif parsed.path == '/api/version':
            self.send_json({'version': START_TIME})
        elif parsed.path == '/api/push/key':
            self.send_json({'publicKey': VAPID_PUBLIC_KEY})
        elif parsed.path == '/api/push/debug':
            subs = get_subscriptions()
            self.send_json({'count': len(subs), 'vapid_key_set': bool(VAPID_PUBLIC_KEY)})
        elif parsed.path == '/api/orders':
            self.send_json(get_orders(params.get('barcode', [''])[0]))
        elif parsed.path == '/api/search':
            products, total = search_products(
                params.get('q', [''])[0], params.get('barcode', [''])[0],
                int(params.get('limit', ['50'])[0]), int(params.get('offset', ['0'])[0]))
            self.send_json({'products': products, 'total': total})
        else:
            self.send_file(parsed.path)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))

        if self.path == '/api/comments':
            new_id = add_comment(body['barcode'], body['content'], body['created_at'], body.get('parent_id'))
            self.send_json({'ok': True, 'id': new_id})
        elif self.path == '/api/comments/delete':
            delete_comment(body['id'])
            self.send_json({'ok': True})
        elif self.path == '/api/push/subscribe':
            ep = body.get('endpoint', '')
            keys = body.get('keys', {})
            print(f'[PUSH] 구독 요청: endpoint={ep[:60]}')
            save_subscription(ep, keys.get('p256dh',''), keys.get('auth',''))
            print(f'[PUSH] 저장 완료. 총 구독자: {len(get_subscriptions())}명')
            self.send_json({'ok': True})
        elif self.path == '/api/orders':
            new_id = add_order(body)
            send_push_notification('새 고객 주문',
                f"{body.get('name','상품')}{' · ' + body.get('customer','') if body.get('customer') else ''}")
            self.send_json({'ok': True, 'id': new_id})
        elif self.path == '/api/orders/update':
            update_order(body)
            self.send_json({'ok': True})
        elif self.path == '/api/orders/delete':
            delete_order(body['id'])
            self.send_json({'ok': True})
        elif self.path == '/api/orders/complete':
            toggle_complete(body['id'])
            self.send_json({'ok': True})
        else:
            self.send_response(404); self.end_headers()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        if path == '/' or path == '':
            path = '/index.html'
        filepath = '.' + path
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            ext = path.split('.')[-1]
            types = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css', 'json': 'application/json'}
            ct = types.get(ext, 'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-Type', ct + '; charset=utf-8')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(DB_PATH):
        import urllib.request
        DB_URL = 'https://github.com/eulgilees/sangpum-catalog/releases/download/v1.0/products.db'
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        print(f'DB 다운로드 중...')
        urllib.request.urlretrieve(DB_URL, DB_PATH)
        print('DB 다운로드 완료!')
    print('PostgreSQL 테이블 초기화...')
    init_pg_tables()
    port = int(os.environ.get('PORT', 8747))
    print(f'서버 시작: http://localhost:{port}')
    HTTPServer(('', port), Handler).serve_forever()
