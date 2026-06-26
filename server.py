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
DATA_DB_PATH = os.environ.get('DATA_DB_PATH', '/data/orders.db')
VAPID_PUBLIC_KEY  = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_EMAIL       = os.environ.get('VAPID_EMAIL', 'mailto:admin@example.com')

def data_db():
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_tables():
    conn = data_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT, content TEXT, created_at TEXT, parent_id INTEGER
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, occurred_at TEXT DEFAULT '', ended_at TEXT DEFAULT '',
        content TEXT DEFAULT '', status TEXT DEFAULT '진행중', created_at TEXT DEFAULT ''
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT, name TEXT, qty INTEGER DEFAULT 1,
        order_date TEXT DEFAULT '', payment TEXT DEFAULT '미불',
        ordered TEXT DEFAULT '미완료', pickup_date TEXT DEFAULT '',
        customer TEXT DEFAULT '', phone TEXT DEFAULT '',
        delivery TEXT DEFAULT '없음', address TEXT DEFAULT '',
        staff TEXT DEFAULT '', note TEXT DEFAULT '',
        created_at TEXT DEFAULT '', completed INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def search_products(query='', barcode='', limit=50, offset=0):
    conn = sqlite3.connect(DB_PATH)
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
    c.execute('SELECT id,content,created_at,parent_id FROM comments WHERE barcode=? ORDER BY id', (barcode,))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    top = [r for r in rows if not r['parent_id']]
    replies = {}
    for r in rows:
        if r['parent_id']: replies.setdefault(r['parent_id'], []).append(r)
    for t in top: t['replies'] = replies.get(t['id'], [])
    return top

def add_comment(barcode, content, created_at, parent_id=None):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO comments(barcode,content,created_at,parent_id) VALUES(?,?,?,?)',
              (barcode, content, created_at, parent_id))
    new_id = c.lastrowid; conn.commit(); conn.close()
    return new_id

def delete_comment(comment_id):
    conn = data_db()
    conn.execute('DELETE FROM comments WHERE id=?', (comment_id,))
    conn.commit(); conn.close()

def search_comments(query):
    pconn = sqlite3.connect(DB_PATH); pconn.row_factory = sqlite3.Row
    products = {r['barcode']: dict(r) for r in pconn.execute('SELECT barcode,name,author,publisher,price FROM products')}
    pconn.close()
    conn = data_db(); c = conn.cursor()
    like = f"%{query.lower().replace(' ','')}%"
    c.execute('''SELECT id,barcode,content,created_at FROM comments
                 WHERE replace(lower(content),' ','') LIKE ? ORDER BY id DESC LIMIT 100''', (like,))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    for r in rows:
        p = products.get(r['barcode'], {})
        r.update({'name': p.get('name',''), 'author': p.get('author',''),
                  'publisher': p.get('publisher',''), 'price': p.get('price',0)})
    return rows

def save_subscription(endpoint, p256dh, auth):
    conn = data_db()
    conn.execute('INSERT OR REPLACE INTO push_subscriptions(endpoint,p256dh,auth) VALUES(?,?,?)',
                 (endpoint, p256dh, auth))
    conn.commit(); conn.close()

def get_subscriptions():
    conn = data_db()
    rows = [dict(r) for r in conn.execute('SELECT * FROM push_subscriptions')]
    conn.close(); return rows

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
    if barcode: c.execute('SELECT * FROM orders WHERE barcode=? ORDER BY id DESC', (barcode,))
    else: c.execute('SELECT * FROM orders ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]; conn.close(); return rows

def add_order(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO orders(barcode,name,qty,order_date,payment,ordered,pickup_date,
                 customer,phone,delivery,address,staff,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (data.get('barcode',''), data.get('name',''), data.get('qty',1),
               data.get('order_date',''), data.get('payment','미불'), data.get('ordered','미완료'),
               data.get('pickup_date',''), data.get('customer',''), data.get('phone',''),
               data.get('delivery','없음'), data.get('address',''),
               data.get('staff',''), data.get('note',''), data.get('created_at','')))
    new_id = c.lastrowid; conn.commit(); conn.close(); return new_id

def update_order(data):
    conn = data_db()
    conn.execute('''UPDATE orders SET qty=?,order_date=?,payment=?,ordered=?,pickup_date=?,
                    customer=?,phone=?,delivery=?,address=?,staff=?,note=? WHERE id=?''',
                 (data.get('qty',1), data.get('order_date',''), data.get('payment','미불'),
                  data.get('ordered','미완료'), data.get('pickup_date',''), data.get('customer',''),
                  data.get('phone',''), data.get('delivery','없음'), data.get('address',''),
                  data.get('staff',''), data.get('note',''), data['id']))
    conn.commit(); conn.close()

def delete_order(order_id):
    conn = data_db()
    conn.execute('DELETE FROM orders WHERE id=?', (order_id,))
    conn.commit(); conn.close()

def get_issues():
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM issues ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]; conn.close(); return rows

def add_issue(data):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO issues(title,occurred_at,ended_at,content,status,created_at) VALUES(?,?,?,?,?,?)',
              (data.get('title',''), data.get('occurred_at',''), data.get('ended_at',''), data.get('content',''), '진행중', data.get('created_at','')))
    new_id = c.lastrowid; conn.commit(); conn.close(); return new_id

def update_issue(data):
    conn = data_db()
    conn.execute('UPDATE issues SET title=?,occurred_at=?,ended_at=?,content=? WHERE id=?',
                 (data.get('title',''), data.get('occurred_at',''), data.get('ended_at',''), data.get('content',''), data['id']))
    conn.commit(); conn.close()

def set_issue_status(issue_id, status):
    conn = data_db()
    conn.execute('UPDATE issues SET status=? WHERE id=?', (status, issue_id))
    conn.commit(); conn.close()

def delete_issue(issue_id):
    conn = data_db()
    conn.execute('DELETE FROM issues WHERE id=?', (issue_id,))
    conn.commit(); conn.close()

def toggle_complete(order_id):
    conn = data_db()
    conn.execute('UPDATE orders SET completed=1-completed WHERE id=?', (order_id,))
    conn.commit(); conn.close()

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
        elif parsed.path == '/api/issues':
            self.send_json(get_issues())
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

        if self.path == '/api/comments':
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
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
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
    # /data 디렉토리 확보
    os.makedirs(os.path.dirname(os.path.abspath(DATA_DB_PATH)), exist_ok=True)
    print(f'데이터 DB 경로: {DATA_DB_PATH}')
    # 상품 DB 없으면 다운로드
    if not os.path.exists(DB_PATH):
        import urllib.request
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        print('상품 DB 다운로드 중...')
        urllib.request.urlretrieve(
            'https://github.com/eulgilees/sangpum-catalog/releases/download/v1.0/products.db', DB_PATH)
        print('다운로드 완료!')
    init_tables()
    port = int(os.environ.get('PORT', 8747))
    print(f'서버 시작: http://localhost:{port}')
    HTTPServer(('', port), Handler).serve_forever()
