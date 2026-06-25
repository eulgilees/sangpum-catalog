#!/usr/bin/env python3
import sqlite3
import json
import urllib.parse
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

IMAGES_DIR = 'images'

DB_PATH = os.environ.get('DB_PATH', 'products.db')

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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO comments(barcode, content, created_at, parent_id) VALUES(?,?,?,?)',
              (barcode, content, created_at, parent_id))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id

def delete_comment(comment_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM comments WHERE id=?', (comment_id,))
    conn.commit()
    conn.close()

def search_comments(query):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    like = f"%{query.lower().replace(' ', '')}%"
    c.execute('''
        SELECT c.id, c.barcode, c.content, c.created_at,
               p.name, p.author, p.publisher, p.price
        FROM comments c
        LEFT JOIN products p ON p.barcode = c.barcode
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
    # 볼륨에 DB가 없으면 GitHub Releases에서 다운로드
    if not os.path.exists(DB_PATH):
        import urllib.request
        DB_URL = 'https://github.com/eulgilees/sangpum-catalog/releases/download/v1.0/products.db'
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        print(f'DB 다운로드 중... ({DB_URL})')
        urllib.request.urlretrieve(DB_URL, DB_PATH)
        print('DB 다운로드 완료!')
    port = int(os.environ.get('PORT', 8747))
    print(f'서버 시작: http://localhost:{port}')
    HTTPServer(('', port), Handler).serve_forever()
