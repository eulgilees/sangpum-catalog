#!/usr/bin/env python3
import sqlite3
import json
import urllib.parse
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

IMAGES_DIR = 'images'
DB_PATH = os.environ.get('DB_PATH', 'products.db')
# 주문·댓글·알림 데이터는 별도 파일에 저장 (배포해도 절대 안 날아감)
DATA_DB_PATH = os.environ.get('DATA_DB_PATH', '/data/orders.db')
VAPID_PUBLIC_KEY  = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_EMAIL       = os.environ.get('VAPID_EMAIL', 'mailto:admin@example.com')

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

def get_comments(barcode):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, content, created_at, parent_id FROM comments WHERE barcode=? ORDER BY id ASC', (barcode,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    # 트리 구조로 변환
    top = [r for r in rows if not r['parent_id']]
    replies = {}
    for r in rows:
        if r['parent_id']:
            replies.setdefault(r['parent_id'], []).append(r)
    for t in top:
        t['replies'] = replies.get(t['id'], [])
    return top

def add_comment(barcode, content, created_at, parent_id=None):
    conn = sqlite3.connect(DATA_DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO comments(barcode, content, created_at, parent_id) VALUES(?,?,?,?)',
              (barcode, content, created_at, parent_id))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id

def delete_comment(comment_id):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.execute('DELETE FROM comments WHERE id=?', (comment_id,))
    conn.commit()
    conn.close()

def init_push_table():
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT
    )''')
    conn.commit()
    conn.close()

def save_subscription(endpoint, p256dh, auth):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.execute('INSERT OR REPLACE INTO push_subscriptions(endpoint,p256dh,auth) VALUES(?,?,?)',
                 (endpoint, p256dh, auth))
    conn.commit()
    conn.close()

def get_subscriptions():
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT * FROM push_subscriptions')]
    conn.close()
    return rows

def send_push_notification(title, body):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        print('VAPID 키 없음, 푸시 스킵')
        return
    try:
        from pywebpush import webpush, WebPushException
        subs = get_subscriptions()
        print(f'푸시 발송: {len(subs)}명, title={title}')
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        'endpoint': sub['endpoint'],
                        'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}
                    },
                    data=json.dumps({'title': title, 'body': body}, ensure_ascii=False),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': VAPID_EMAIL}
                )
                print(f'푸시 성공: {sub["endpoint"][:40]}...')
            except WebPushException as e:
                print(f'푸시 실패: {e}')
    except Exception as e:
        print(f'푸시 오류: {e}')

def init_orders_table():
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT, name TEXT, qty INTEGER DEFAULT 1,
        order_date TEXT, payment TEXT DEFAULT '미불', ordered TEXT DEFAULT '미완료',
        pickup_date TEXT, customer TEXT, phone TEXT, delivery TEXT DEFAULT '없음',
        address TEXT, staff TEXT, note TEXT, created_at TEXT, completed INTEGER DEFAULT 0
    )''')
    # 기존 DB에 빠진 컬럼 자동 추가
    existing = {row[1] for row in conn.execute('PRAGMA table_info(orders)')}
    migrations = [
        ('order_date',  'TEXT DEFAULT ""'),
        ('customer',    'TEXT DEFAULT ""'),
        ('phone',       'TEXT DEFAULT ""'),
        ('delivery',    'TEXT DEFAULT "없음"'),
        ('address',     'TEXT DEFAULT ""'),
        ('completed',   'INTEGER DEFAULT 0'),
    ]
    for col, typedef in migrations:
        if col not in existing:
            conn.execute(f'ALTER TABLE orders ADD COLUMN {col} {typedef}')
    conn.commit()
    conn.close()

def get_orders(barcode=''):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if barcode:
        c.execute('SELECT * FROM orders WHERE barcode=? ORDER BY id DESC', (barcode,))
    else:
        c.execute('SELECT * FROM orders ORDER BY id DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def add_order(data):
    conn = sqlite3.connect(DATA_DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO orders(barcode,name,qty,order_date,payment,ordered,pickup_date,customer,phone,delivery,address,staff,note,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (data.get('barcode',''), data.get('name',''), data.get('qty',1),
               data.get('order_date',''), data.get('payment','미불'), data.get('ordered','미완료'),
               data.get('pickup_date',''), data.get('customer',''), data.get('phone',''),
               data.get('delivery','없음'), data.get('address',''),
               data.get('staff',''), data.get('note',''), data.get('created_at','')))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id

def update_order(data):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.execute('''UPDATE orders SET qty=?,order_date=?,payment=?,ordered=?,pickup_date=?,customer=?,phone=?,delivery=?,address=?,staff=?,note=?
                    WHERE id=?''',
                 (data.get('qty',1), data.get('order_date',''), data.get('payment','미불'), data.get('ordered','미완료'),
                  data.get('pickup_date',''), data.get('customer',''), data.get('phone',''),
                  data.get('delivery','없음'), data.get('address',''),
                  data.get('staff',''), data.get('note',''), data['id']))
    conn.commit()
    conn.close()

def delete_order(order_id):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.execute('DELETE FROM orders WHERE id=?', (order_id,))
    conn.commit()
    conn.close()

def search_comments(query):
    conn = sqlite3.connect(DATA_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{DB_PATH}' AS products_db")
    c = conn.cursor()
    like = f"%{query.lower().replace(' ', '')}%"
    c.execute('''
        SELECT c.id, c.barcode, c.content, c.created_at,
               p.name, p.author, p.publisher, p.price
        FROM comments c
        LEFT JOIN products_db.products p ON p.barcode = c.barcode
        WHERE replace(lower(c.content), ' ', '') LIKE ?
        ORDER BY c.id DESC
        LIMIT 100
    ''', (like,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

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

        elif parsed.path == '/api/push/key':
            self.send_json({'publicKey': VAPID_PUBLIC_KEY})

        elif parsed.path == '/api/push/debug':
            subs = get_subscriptions()
            self.send_json({'count': len(subs), 'vapid_key_set': bool(VAPID_PUBLIC_KEY)})

        elif parsed.path == '/api/orders':
            barcode = params.get('barcode', [''])[0]
            self.send_json(get_orders(barcode))

        elif parsed.path == '/api/comments/search':
            query = params.get('q', [''])[0]
            self.send_json(search_comments(query))

        elif parsed.path == '/api/search':
            query = params.get('q', [''])[0]
            barcode = params.get('barcode', [''])[0]
            limit = int(params.get('limit', ['50'])[0])
            offset = int(params.get('offset', ['0'])[0])
            products, total = search_products(query, barcode, limit, offset)
            self.send_json({'products': products, 'total': total, 'offset': offset, 'limit': limit})

        else:
            self.send_file(parsed.path)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))

        if self.path == '/api/image/upload':
            ct = self.headers.get('Content-Type', '')
            boundary = ct.split('boundary=')[-1].encode()
            body_raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            # 바코드 파싱
            barcode_match = re.search(rb'name="barcode"\r\n\r\n([^\r\n]+)', body_raw)
            if not barcode_match:
                self.send_response(400); self.end_headers(); return
            barcode = re.sub(r'[^\w\-]', '', barcode_match.group(1).decode())
            # 파일 데이터 파싱
            file_match = re.search(rb'Content-Type: (image/\S+)\r\n\r\n(.*?)\r\n--', body_raw, re.DOTALL)
            if not file_match:
                self.send_response(400); self.end_headers(); return
            mime = file_match.group(1).decode()
            ext = mime.split('/')[-1].replace('jpeg','jpg')
            img_data = file_match.group(2)
            # 기존 이미지 삭제
            for old_ext in ['jpg','jpeg','png','webp']:
                old = os.path.join(IMAGES_DIR, f'{barcode}.{old_ext}')
                if os.path.exists(old): os.remove(old)
            save_path = os.path.join(IMAGES_DIR, f'{barcode}.{ext}')
            with open(save_path, 'wb') as f:
                f.write(img_data)
            self.send_json({'ok': True, 'path': f'/api/image?barcode={barcode}'})

        elif self.path == '/api/comments':
            new_id = add_comment(body['barcode'], body['content'], body['created_at'], body.get('parent_id'))
            self.send_json({'ok': True, 'id': new_id})

        elif self.path == '/api/comments/delete':
            delete_comment(body['id'])
            self.send_json({'ok': True})

        elif self.path == '/api/push/subscribe':
            ep = body.get('endpoint', '')
            keys = body.get('keys', {})
            print(f'[PUSH] 구독 요청: endpoint={ep[:60]}')
            print(f'[PUSH] keys 존재: p256dh={bool(keys.get("p256dh"))}, auth={bool(keys.get("auth"))}')
            save_subscription(ep, keys.get('p256dh',''), keys.get('auth',''))
            total = len(get_subscriptions())
            print(f'[PUSH] 저장 완료. 총 구독자: {total}명')
            self.send_json({'ok': True})

        elif self.path == '/api/orders':
            new_id = add_order(body)
            name = body.get('name', '상품')
            customer = body.get('customer', '')
            send_push_notification(
                '새 고객 주문',
                f"{name}{' · ' + customer if customer else ''}"
            )
            self.send_json({'ok': True, 'id': new_id})

        elif self.path == '/api/orders/update':
            update_order(body)
            self.send_json({'ok': True})

        elif self.path == '/api/orders/delete':
            delete_order(body['id'])
            self.send_json({'ok': True})

        elif self.path == '/api/orders/complete':
            conn = sqlite3.connect(DATA_DB_PATH)
            conn.execute('UPDATE orders SET completed=1-completed WHERE id=?', (body['id'],))
            conn.commit()
            conn.close()
            self.send_json({'ok': True})

        else:
            self.send_response(404)
            self.end_headers()

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
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # 주문 데이터 디렉토리 확보
    os.makedirs(os.path.dirname(os.path.abspath(DATA_DB_PATH)), exist_ok=True)
    # 볼륨에 DB가 없으면 GitHub Releases에서 다운로드
    if not os.path.exists(DB_PATH):
        import urllib.request
        DB_URL = 'https://github.com/eulgilees/sangpum-catalog/releases/download/v1.0/products.db'
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        print(f'DB 다운로드 중... ({DB_URL})')
        urllib.request.urlretrieve(DB_URL, DB_PATH)
        print('DB 다운로드 완료!')
    init_push_table()
    init_orders_table()
    port = int(os.environ.get('PORT', 8747))
    print(f'서버 시작: http://localhost:{port}')
    HTTPServer(('', port), Handler).serve_forever()
