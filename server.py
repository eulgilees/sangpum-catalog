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
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

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
        content TEXT DEFAULT '', status TEXT DEFAULT '진행중', created_at TEXT DEFAULT '',
        store TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        barcode TEXT, name TEXT, qty INTEGER DEFAULT 1,
        order_date TEXT DEFAULT '', payment TEXT DEFAULT '미불',
        ordered TEXT DEFAULT '미완료', pickup_date TEXT DEFAULT '',
        customer TEXT DEFAULT '', phone TEXT DEFAULT '',
        delivery TEXT DEFAULT '없음', address TEXT DEFAULT '',
        staff TEXT DEFAULT '', note TEXT DEFAULT '',
        created_at TEXT DEFAULT '', completed INTEGER DEFAULT 0,
        store TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS as_requests (
        id SERIAL PRIMARY KEY,
        received_date TEXT DEFAULT '', product_name TEXT DEFAULT '',
        content TEXT DEFAULT '', customer TEXT DEFAULT '', phone TEXT DEFAULT '',
        delivery TEXT DEFAULT '없음', staff TEXT DEFAULT '',
        note TEXT DEFAULT '', status TEXT DEFAULT '진행중', created_at TEXT DEFAULT '',
        store TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS as_logs (
        id SERIAL PRIMARY KEY,
        as_id INTEGER NOT NULL,
        log_date TEXT DEFAULT '',
        content TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS suggestions (
        id SERIAL PRIMARY KEY,
        content TEXT DEFAULT '', date TEXT DEFAULT '',
        status TEXT DEFAULT '미처리', created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS suggestion_comments (
        id SERIAL PRIMARY KEY,
        suggestion_id INTEGER NOT NULL,
        display_name TEXT DEFAULT '',
        content TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        store TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )''')
    # 마이그레이션
    try:
        conn2 = data_db(); c2 = conn2.cursor()
        c2.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT DEFAULT ''")
        c2.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS store TEXT DEFAULT ''")
        c2.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS store TEXT DEFAULT ''")
        c2.execute("ALTER TABLE issues ADD COLUMN IF NOT EXISTS store TEXT DEFAULT ''")
        c2.execute("ALTER TABLE as_requests ADD COLUMN IF NOT EXISTS store TEXT DEFAULT ''")
        c2.execute("UPDATE orders SET store='잠실점' WHERE store=''")
        c2.execute("UPDATE issues SET store='잠실점' WHERE store=''")
        c2.execute("UPDATE as_requests SET store='잠실점' WHERE store=''")
        conn2.commit(); conn2.close()
    except: pass
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires_at BIGINT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_rooms (
        id SERIAL PRIMARY KEY,
        created_at BIGINT DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_room_members (
        room_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_read BIGINT DEFAULT 0,
        PRIMARY KEY (room_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id SERIAL PRIMARY KEY,
        room_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        display_name TEXT DEFAULT '',
        content TEXT NOT NULL,
        created_at BIGINT DEFAULT 0
    )''')
    # push_subscriptions에 user_id 컬럼 추가 (채팅 알림 대상 특정용)
    try:
        conn2 = data_db(); c2 = conn2.cursor()
        c2.execute("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS user_id INTEGER DEFAULT NULL")
        conn2.commit(); conn2.close()
    except: pass
    # chat_rooms에 그룹 채팅 컬럼 추가
    try:
        conn3 = data_db(); c3 = conn3.cursor()
        c3.execute("ALTER TABLE chat_rooms ADD COLUMN IF NOT EXISTS is_group BOOLEAN DEFAULT FALSE")
        c3.execute("ALTER TABLE chat_rooms ADD COLUMN IF NOT EXISTS group_name TEXT DEFAULT ''")
        conn3.commit(); conn3.close()
    except: pass
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

def save_subscription(endpoint, p256dh, auth, user_id=None):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO push_subscriptions(endpoint,p256dh,auth,user_id) VALUES(%s,%s,%s,%s)
                 ON CONFLICT(endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth, user_id=EXCLUDED.user_id''',
              (endpoint, p256dh, auth, user_id))
    conn.commit(); conn.close()

def get_subscriptions(user_id=None):
    conn = data_db(); c = conn.cursor()
    if user_id is not None:
        c.execute('SELECT * FROM push_subscriptions WHERE user_id=%s', (user_id,))
    else:
        # 로그인된 사용자(user_id 있는)의 구독만 반환
        c.execute('SELECT * FROM push_subscriptions WHERE user_id IS NOT NULL')
    rows = rows_to_dicts(c); conn.close(); return rows

def send_push_notification(title, body, target_user_id=None, tag='sangpum', url='/'):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        print('VAPID 키 없음'); return
    try:
        from pywebpush import webpush, WebPushException
        subs = get_subscriptions(target_user_id)
        print(f'푸시 발송: {len(subs)}명')
        payload = json.dumps({'title': title, 'body': body, 'tag': tag, 'url': url}, ensure_ascii=False)
        for sub in subs:
            try:
                webpush(subscription_info={'endpoint': sub['endpoint'],
                                           'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}},
                        data=payload,
                        vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={'sub': VAPID_EMAIL})
            except WebPushException as e:
                print(f'푸시 실패: {e}')
    except Exception as e:
        print(f'푸시 오류: {e}')

# ── 채팅 함수 ──
def get_all_users():
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT id, display_name, store FROM users ORDER BY display_name')
    rows = rows_to_dicts(c); conn.close(); return rows

def create_group_room(creator_id, member_ids, group_name):
    conn = data_db(); c = conn.cursor()
    c.execute("INSERT INTO chat_rooms (created_at, is_group, group_name) VALUES (%s, TRUE, %s) RETURNING id",
              (int(time.time()), group_name))
    rid = c.fetchone()[0]
    all_members = list(set([creator_id] + member_ids))
    for uid in all_members:
        c.execute("INSERT INTO chat_room_members (room_id, user_id, last_read) VALUES (%s, %s, 0)", (rid, uid))
    conn.commit(); conn.close(); return rid

def chat_get_other_member_ids(room_id, my_user_id):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT user_id FROM chat_room_members WHERE room_id=%s AND user_id != %s', (room_id, my_user_id))
    rows = c.fetchall(); conn.close()
    return [row[0] for row in rows]

def get_or_create_dm_room(user_id1, user_id2):
    conn = data_db(); c = conn.cursor()
    c.execute('''SELECT a.room_id FROM chat_room_members a
                 JOIN chat_room_members b ON a.room_id=b.room_id AND b.user_id=%s
                 WHERE a.user_id=%s''', (user_id2, user_id1))
    rows = c.fetchall()
    for row in rows:
        rid = row[0]
        c.execute('SELECT COUNT(*) FROM chat_room_members WHERE room_id=%s', (rid,))
        if c.fetchone()[0] == 2:
            conn.close(); return rid
    c.execute('INSERT INTO chat_rooms (created_at) VALUES (%s) RETURNING id', (int(time.time()),))
    rid = c.fetchone()[0]
    c.execute('INSERT INTO chat_room_members (room_id, user_id, last_read) VALUES (%s,%s,0),(%s,%s,0)',
              (rid, user_id1, rid, user_id2))
    conn.commit(); conn.close(); return rid

def get_my_rooms(user_id):
    conn = data_db(); c = conn.cursor()
    c.execute('''
        SELECT r.id, COALESCE(r.is_group, FALSE) as is_group, COALESCE(r.group_name, '') as group_name,
               (SELECT u.display_name FROM users u
                JOIN chat_room_members m2 ON m2.user_id=u.id
                WHERE m2.room_id=r.id AND m2.user_id != %s LIMIT 1) as other_name,
               (SELECT u.store FROM users u
                JOIN chat_room_members m2 ON m2.user_id=u.id
                WHERE m2.room_id=r.id AND m2.user_id != %s LIMIT 1) as other_store,
               (SELECT content FROM chat_messages WHERE room_id=r.id ORDER BY id DESC LIMIT 1) as last_msg,
               (SELECT created_at FROM chat_messages WHERE room_id=r.id ORDER BY id DESC LIMIT 1) as last_ts,
               (SELECT COUNT(*) FROM chat_messages
                WHERE room_id=r.id AND created_at >
                    (SELECT last_read FROM chat_room_members WHERE room_id=r.id AND user_id=%s)) as unread,
               (SELECT COUNT(*) FROM chat_room_members WHERE room_id=r.id) as member_count
        FROM chat_rooms r
        JOIN chat_room_members m ON m.room_id=r.id AND m.user_id=%s
        ORDER BY last_ts DESC NULLS LAST
    ''', (user_id, user_id, user_id, user_id))
    rooms = rows_to_dicts(c)
    # 그룹 채팅의 경우 멤버 이름 목록 추가
    for room in rooms:
        if room.get('is_group'):
            c.execute('''SELECT u.display_name FROM users u
                         JOIN chat_room_members m ON m.user_id=u.id
                         WHERE m.room_id=%s AND u.id != %s''', (room['id'], user_id))
            room['member_names'] = [row[0] for row in c.fetchall()]
        else:
            room['member_names'] = []
    conn.close(); return rooms

def get_messages(room_id, after=0, limit=100):
    conn = data_db(); c = conn.cursor()
    c.execute('''SELECT id, user_id, display_name, content, created_at
                 FROM chat_messages WHERE room_id=%s AND id > %s
                 ORDER BY id DESC LIMIT %s''', (room_id, after, limit))
    rows = list(reversed(rows_to_dicts(c))); conn.close(); return rows

def chat_send_message(room_id, user_id, display_name, content):
    conn = data_db(); c = conn.cursor()
    ts = int(time.time())
    c.execute('''INSERT INTO chat_messages (room_id, user_id, display_name, content, created_at)
                 VALUES (%s,%s,%s,%s,%s) RETURNING id''',
              (room_id, user_id, display_name, content, ts))
    msg_id = c.fetchone()[0]
    c.execute('UPDATE chat_room_members SET last_read=%s WHERE room_id=%s AND user_id=%s',
              (ts, room_id, user_id))
    conn.commit(); conn.close()
    return {'id': msg_id, 'room_id': room_id, 'user_id': user_id,
            'display_name': display_name, 'content': content, 'created_at': ts}

def chat_mark_read(room_id, user_id):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE chat_room_members SET last_read=%s WHERE room_id=%s AND user_id=%s',
              (int(time.time()), room_id, user_id))
    conn.commit(); conn.close()

def chat_get_other_user_id(room_id, my_user_id):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT user_id FROM chat_room_members WHERE room_id=%s AND user_id != %s LIMIT 1',
              (room_id, my_user_id))
    row = c.fetchone(); conn.close()
    return row[0] if row else None

def chat_total_unread(user_id):
    conn = data_db(); c = conn.cursor()
    c.execute('''SELECT COALESCE(SUM(cnt),0) FROM (
        SELECT COUNT(*) as cnt FROM chat_messages cm
        JOIN chat_room_members rm ON rm.room_id=cm.room_id AND rm.user_id=%s
        WHERE cm.created_at > rm.last_read AND cm.user_id != %s
    ) t''', (user_id, user_id))
    row = c.fetchone(); conn.close()
    return int(row[0]) if row else 0

def get_orders(store='', barcode=''):
    conn = data_db(); c = conn.cursor()
    if barcode:
        c.execute('SELECT * FROM orders WHERE barcode=%s AND store=%s ORDER BY completed, id DESC', (barcode, store))
    else:
        c.execute('SELECT * FROM orders WHERE store=%s ORDER BY completed, id DESC', (store,))
    rows = rows_to_dicts(c); conn.close(); return rows

def add_order(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO orders(barcode,name,qty,order_date,payment,ordered,pickup_date,
                 customer,phone,delivery,address,staff,note,created_at,store) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
              (data.get('barcode',''), data.get('name',''), data.get('qty',1),
               data.get('order_date',''), data.get('payment','미불'), data.get('ordered','미완료'),
               data.get('pickup_date',''), data.get('customer',''), data.get('phone',''),
               data.get('delivery','없음'), data.get('address',''),
               data.get('staff',''), data.get('note',''), data.get('created_at',''),
               data.get('store','')))
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

def get_issues(store=''):
    conn = data_db(); c = conn.cursor()
    c.execute("SELECT * FROM issues WHERE store=%s ORDER BY CASE WHEN status='종료' THEN 1 ELSE 0 END, id DESC", (store,))
    rows = rows_to_dicts(c); conn.close(); return rows

def add_issue(data):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO issues(title,occurred_at,ended_at,content,status,created_at,store) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
              (data.get('title',''), data.get('occurred_at',''), data.get('ended_at',''),
               data.get('content',''), '진행중', data.get('created_at',''), data.get('store','')))
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

def get_as_requests(store=''):
    conn = data_db(); c = conn.cursor()
    c.execute("SELECT * FROM as_requests WHERE store=%s ORDER BY CASE WHEN status='완료' THEN 1 ELSE 0 END, id DESC", (store,))
    rows = rows_to_dicts(c); conn.close(); return rows

def add_as_request(data):
    conn = data_db(); c = conn.cursor()
    c.execute('''INSERT INTO as_requests(received_date,product_name,content,customer,phone,delivery,staff,note,status,created_at,store)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
              (data.get('received_date',''), data.get('product_name',''), data.get('content',''),
               data.get('customer',''), data.get('phone',''), data.get('delivery','없음'),
               data.get('staff',''), data.get('note',''), '진행중', data.get('created_at',''),
               data.get('store','')))
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
    c.execute('DELETE FROM as_logs WHERE as_id=%s', (as_id,))
    conn.commit(); conn.close()

def get_as_logs(as_id):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM as_logs WHERE as_id=%s ORDER BY log_date ASC, id ASC', (as_id,))
    rows = rows_to_dicts(c); conn.close(); return rows

def add_as_log(as_id, log_date, content):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO as_logs(as_id, log_date, content, created_at) VALUES(%s,%s,%s,%s) RETURNING id',
              (as_id, log_date, content, datetime.now().isoformat()))
    row = c.fetchone(); conn.commit(); conn.close()
    return row[0]

def delete_as_log(log_id):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM as_logs WHERE id=%s', (log_id,))
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
    c.execute('DELETE FROM suggestion_comments WHERE suggestion_id=%s', (sid,))
    c.execute('DELETE FROM suggestions WHERE id=%s', (sid,))
    conn.commit(); conn.close()

def get_suggestion_comments(suggestion_id):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT * FROM suggestion_comments WHERE suggestion_id=%s ORDER BY id', (suggestion_id,))
    rows = rows_to_dicts(c); conn.close(); return rows

def add_suggestion_comment(data):
    conn = data_db(); c = conn.cursor()
    c.execute('INSERT INTO suggestion_comments(suggestion_id,display_name,content,created_at) VALUES(%s,%s,%s,%s) RETURNING id',
              (data.get('suggestion_id'), data.get('display_name',''), data.get('content',''), data.get('created_at','')))
    new_id = c.fetchone()[0]; conn.commit(); conn.close(); return new_id

def delete_suggestion_comment(cid):
    conn = data_db(); c = conn.cursor()
    c.execute('DELETE FROM suggestion_comments WHERE id=%s', (cid,))
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

def register_user(username, password, display_name, phone, store=''):
    conn = data_db(); c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password_hash, display_name, phone, store, created_at) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
                  (username.strip(), hash_password(password), display_name.strip(), phone.strip(), store.strip(), str(int(time.time()))))
        user_id = c.fetchone()[0]
        conn.commit()
        return {'id': user_id, 'username': username, 'display_name': display_name, 'store': store}
    except Exception as e:
        conn.rollback(); raise e
    finally: conn.close()

def login_user(username, password):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT id, username, display_name, password_hash, phone, store FROM users WHERE username=%s', (username.strip(),))
    rows = rows_to_dicts(c); conn.close()
    if not rows: return None
    u = rows[0]
    if not verify_password(password, u['password_hash']): return None
    return {'id': u['id'], 'username': u['username'], 'display_name': u['display_name'], 'phone': u['phone'], 'store': u['store']}

def find_user_by_phone(username, phone):
    conn = data_db(); c = conn.cursor()
    c.execute('SELECT id, username, display_name FROM users WHERE username=%s AND phone=%s',
              (username.strip(), phone.strip()))
    rows = rows_to_dicts(c); conn.close()
    return rows[0] if rows else None

def reset_password(user_id, new_password):
    conn = data_db(); c = conn.cursor()
    c.execute('UPDATE users SET password_hash=%s WHERE id=%s', (hash_password(new_password), user_id))
    conn.commit(); conn.close()

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
    c.execute('''SELECT u.id, u.username, u.display_name, u.phone, u.store FROM sessions s
                 JOIN users u ON u.id = s.user_id
                 WHERE s.token=%s AND s.expires_at > %s''', (token, int(time.time())))
    rows = rows_to_dicts(c); conn.close()
    return rows[0] if rows else None

def update_profile(user_id, display_name, phone, store, new_password=None):
    conn = data_db(); c = conn.cursor()
    if new_password:
        c.execute('UPDATE users SET display_name=%s, phone=%s, store=%s, password_hash=%s WHERE id=%s',
                  (display_name.strip(), phone.strip(), store.strip(), hash_password(new_password), user_id))
    else:
        c.execute('UPDATE users SET display_name=%s, phone=%s, store=%s WHERE id=%s',
                  (display_name.strip(), phone.strip(), store.strip(), user_id))
    conn.commit(); conn.close()

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
            user = verify_session(self.headers.get('X-Token',''))
            self.send_json(get_as_requests(user['store'] if user else ''))
        elif parsed.path == '/api/as/logs':
            as_id = int(params.get('as_id', ['0'])[0])
            self.send_json({'ok': True, 'logs': get_as_logs(as_id)})
        elif parsed.path == '/api/suggestions':
            self.send_json(get_suggestions())
        elif parsed.path == '/api/suggestions/comments':
            sid = parsed.query.split('suggestion_id=')[-1].split('&')[0] if 'suggestion_id=' in parsed.query else None
            if sid: self.send_json(get_suggestion_comments(int(sid)))
            else: self.send_json([])
        elif parsed.path == '/api/issues':
            user = verify_session(self.headers.get('X-Token',''))
            self.send_json(get_issues(user['store'] if user else ''))
        elif parsed.path == '/api/auth/me':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if user: self.send_json({'ok': True, 'user': user})
            else: self.send_json({'ok': False})
        elif parsed.path == '/api/chat/users':
            token = self.headers.get('X-Token','')
            if not verify_session(token): self.send_json({'ok': False}); return
            self.send_json({'ok': True, 'users': get_all_users()})
        elif parsed.path == '/api/chat/rooms':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False}); return
            rooms = get_my_rooms(user['id'])
            total = sum(r.get('unread', 0) for r in rooms)
            self.send_json({'ok': True, 'rooms': rooms, 'unread': total})
        elif parsed.path == '/api/chat/messages':
            token = self.headers.get('X-Token','')
            room_id = int(params.get('room_id', ['0'])[0])
            after = int(params.get('after', ['0'])[0])
            # DB 연결 1번으로 세션확인+메시지조회+읽음처리 한번에
            conn = data_db(); c = conn.cursor()
            try:
                c.execute('''SELECT u.id, u.username, u.display_name, u.phone, u.store
                             FROM sessions s JOIN users u ON u.id=s.user_id
                             WHERE s.token=%s AND s.expires_at>%s''', (token, int(time.time())))
                rows = rows_to_dicts(c)
                if not rows: self.send_json({'ok': False}); return
                user = rows[0]
                c.execute('''SELECT id, user_id, display_name, content, created_at
                             FROM chat_messages WHERE room_id=%s AND id>%s
                             ORDER BY id DESC LIMIT 100''', (room_id, after))
                msgs = list(reversed(rows_to_dicts(c)))
                c.execute('UPDATE chat_room_members SET last_read=%s WHERE room_id=%s AND user_id=%s',
                          (int(time.time()), room_id, user['id']))
                conn.commit()
                self.send_json({'ok': True, 'messages': msgs})
            finally:
                conn.close()
        elif parsed.path == '/api/schedule':
            try:
                now = time.localtime()
                year_short = params.get('year', [str(now.tm_year % 100)])[0]
                month = params.get('month', [str(now.tm_mon)])[0]
                self.send_json({'ok': True, 'data': fetch_schedule(year_short, month)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})
        elif parsed.path == '/api/orders':
            user = verify_session(self.headers.get('X-Token',''))
            self.send_json(get_orders(user['store'] if user else '', params.get('barcode',[''])[0]))
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
                phone = re.sub(r'[^\d]', '', body.get('phone',''))
                store = body.get('store','').strip()
                if not username or not password or not display_name or not phone or not store:
                    self.send_json({'ok': False, 'error': '모든 항목을 입력해주세요'}); return
                if not re.fullmatch(r'\d{5}', username):
                    self.send_json({'ok': False, 'error': '사번은 숫자 5자리여야 합니다'}); return
                if len(password) < 4:
                    self.send_json({'ok': False, 'error': '비밀번호는 4자 이상이어야 합니다'}); return
                user = register_user(username, password, display_name, phone, store)
                token = create_session(user['id'])
                self.send_json({'ok': True, 'token': token, 'user': user})
            except Exception as e:
                msg = str(e)
                if 'unique' in msg.lower() or 'duplicate' in msg.lower():
                    self.send_json({'ok': False, 'error': '이미 등록된 사번입니다'})
                else:
                    self.send_json({'ok': False, 'error': '오류가 발생했습니다'})
        elif self.path == '/api/auth/login':
            user = login_user(body.get('username',''), body.get('password',''))
            if not user: self.send_json({'ok': False, 'error': '사번 또는 비밀번호가 틀렸습니다'}); return
            token = create_session(user['id'])
            self.send_json({'ok': True, 'token': token, 'user': user})
        elif self.path == '/api/auth/find-password':
            username = body.get('username','').strip()
            phone = re.sub(r'[^\d]', '', body.get('phone',''))
            user = find_user_by_phone(username, phone)
            if not user: self.send_json({'ok': False, 'error': '사번과 휴대폰 번호가 일치하지 않습니다'}); return
            self.send_json({'ok': True, 'user_id': user['id'], 'display_name': user['display_name']})
        elif self.path == '/api/auth/reset-password':
            user_id = body.get('user_id')
            new_pw = body.get('password','')
            if not user_id or len(new_pw) < 4:
                self.send_json({'ok': False, 'error': '비밀번호는 4자 이상이어야 합니다'}); return
            reset_password(user_id, new_pw)
            self.send_json({'ok': True})
        elif self.path == '/api/auth/update-profile':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False, 'error': '로그인이 필요합니다'}); return
            display_name = body.get('display_name','').strip()
            phone = re.sub(r'[^\d]', '', body.get('phone',''))
            store = body.get('store','').strip()
            new_pw = body.get('new_password','').strip()
            if not display_name or not phone or not store:
                self.send_json({'ok': False, 'error': '모든 항목을 입력해주세요'}); return
            if new_pw and len(new_pw) < 4:
                self.send_json({'ok': False, 'error': '비밀번호는 4자 이상이어야 합니다'}); return
            update_profile(user['id'], display_name, phone, store, new_pw or None)
            updated = verify_session(token)
            self.send_json({'ok': True, 'user': updated})
        elif self.path == '/api/auth/logout':
            delete_session(self.headers.get('X-Token',''))
            self.send_json({'ok': True})
        elif self.path == '/api/chat/room':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False}); return
            other_id = body.get('other_user_id')
            if not other_id: self.send_json({'ok': False, 'error': '상대방을 선택해주세요'}); return
            room_id = get_or_create_dm_room(user['id'], int(other_id))
            self.send_json({'ok': True, 'room_id': room_id})
        elif self.path == '/api/chat/group':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False}); return
            member_ids = body.get('member_ids', [])
            group_name = body.get('group_name', '').strip()
            if len(member_ids) < 1: self.send_json({'ok': False, 'error': '멤버를 선택해주세요'}); return
            if not group_name: self.send_json({'ok': False, 'error': '그룹 이름을 입력해주세요'}); return
            room_id = create_group_room(user['id'], [int(m) for m in member_ids], group_name)
            self.send_json({'ok': True, 'room_id': room_id})
        elif self.path == '/api/chat/message':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False}); return
            room_id = body.get('room_id')
            content = body.get('content','').strip()
            if not room_id or not content: self.send_json({'ok': False}); return
            msg = chat_send_message(int(room_id), user['id'], user['display_name'], content)
            other_uids = chat_get_other_member_ids(int(room_id), user['id'])
            if other_uids:
                import threading
                def push_all(uids):
                    for uid in uids:
                        send_push_notification(f'💬 {user["display_name"]}', content, uid, f'chat-{room_id}', f'/?room={room_id}')
                threading.Thread(target=push_all, args=(other_uids,), daemon=True).start()
            self.send_json({'ok': True, 'message': msg})
        elif self.path == '/api/chat/read':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False}); return
            chat_mark_read(body.get('room_id'), user['id'])
            self.send_json({'ok': True})
        elif self.path == '/api/chat/leave':
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            if not user: self.send_json({'ok': False}); return
            room_id = body.get('room_id')
            if not room_id: self.send_json({'ok': False}); return
            conn = data_db(); c = conn.cursor()
            c.execute('DELETE FROM chat_room_members WHERE room_id=%s AND user_id=%s', (int(room_id), user['id']))
            # 방에 멤버가 없으면 방과 메시지도 삭제
            c.execute('SELECT COUNT(*) FROM chat_room_members WHERE room_id=%s', (int(room_id),))
            if c.fetchone()[0] == 0:
                c.execute('DELETE FROM chat_messages WHERE room_id=%s', (int(room_id),))
                c.execute('DELETE FROM chat_rooms WHERE id=%s', (int(room_id),))
            conn.commit(); conn.close()
            self.send_json({'ok': True})
        elif self.path == '/api/comments':
            self.send_json({'ok': True, 'id': add_comment(body['barcode'], body['content'], body['created_at'], body.get('parent_id'))})
        elif self.path == '/api/comments/delete':
            delete_comment(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/push/subscribe':
            keys = body.get('keys', {})
            token = self.headers.get('X-Token','')
            user = verify_session(token)
            uid = user['id'] if user else None
            save_subscription(body.get('endpoint',''), keys.get('p256dh',''), keys.get('auth',''), uid)
            print(f'[PUSH] 구독 저장. 총: {len(get_subscriptions())}명')
            self.send_json({'ok': True})
        elif self.path == '/api/orders':
            user = verify_session(self.headers.get('X-Token',''))
            body['store'] = user['store'] if user else ''
            new_id = add_order(body)
            send_push_notification('새 고객 주문',
                f"{body.get('name','')}{' · '+body.get('customer','') if body.get('customer') else ''}",
                tag='order')
            self.send_json({'ok': True, 'id': new_id})
        elif self.path == '/api/orders/update':
            update_order(body); self.send_json({'ok': True})
        elif self.path == '/api/orders/delete':
            delete_order(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/orders/complete':
            toggle_complete(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/as':
            user = verify_session(self.headers.get('X-Token',''))
            body['store'] = user['store'] if user else ''
            self.send_json({'ok': True, 'id': add_as_request(body)})
        elif self.path == '/api/as/update':
            update_as_request(body); self.send_json({'ok': True})
        elif self.path == '/api/as/status':
            set_as_status(body['id'], body['status']); self.send_json({'ok': True})
        elif self.path == '/api/as/delete':
            delete_as_request(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/as/log':
            log_id = add_as_log(body['as_id'], body['log_date'], body['content'])
            self.send_json({'ok': True, 'id': log_id})
        elif self.path == '/api/as/log/delete':
            delete_as_log(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/suggestions':
            self.send_json({'ok': True, 'id': add_suggestion(body)})
        elif self.path == '/api/suggestions/status':
            set_suggestion_status(body['id'], body['status']); self.send_json({'ok': True})
        elif self.path == '/api/suggestions/delete':
            delete_suggestion(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/suggestions/comment':
            self.send_json({'ok': True, 'id': add_suggestion_comment(body)})
        elif self.path == '/api/suggestions/comment/delete':
            delete_suggestion_comment(body['id']); self.send_json({'ok': True})
        elif self.path == '/api/issues':
            user = verify_session(self.headers.get('X-Token',''))
            body['store'] = user['store'] if user else ''
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
    ThreadedHTTPServer(('', port), Handler).serve_forever()
