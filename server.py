"""Listen Lab: durable, loopback-only media library and local speech recognition."""
import http.client
import importlib.util
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import re
import socket
import threading
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LISTEN_LAB_DATA_DIR', ROOT / 'data')).resolve()
PORT = int(os.environ.get('LISTEN_LAB_PORT', '8765'))
MAX_BYTES = 500 * 1024 * 1024
EXTENSIONS = {'.mp4', '.webm', '.mov', '.m4v', '.mkv', '.mp3', '.m4a', '.wav', '.ogg', '.aac', '.flac'}
RECORDS = {}
LOCK = threading.RLock()
JOBS = queue.Queue()
MODEL = None
ACTIVE_JOBS = set()
RETENTION = timedelta(days=30)
ID_RE = r'[a-f0-9]{32}'


class UserError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def word_key(value):
    return unicodedata.normalize('NFKC', value).casefold().replace('’', "'").replace('‘', "'")


def persist(item):
    """Caller holds LOCK; atomic replace retains previous complete record on failure."""
    path = DATA / 'records' / (item['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(item, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    RECORDS[item['id']] = item


def mutate(video_id, changes, revision=None):
    with LOCK:
        item = RECORDS.get(video_id)
        if not item or item.get('deleted_at') or (revision is not None and item['revision'] != revision):
            return None
        item = {**item, **changes, 'updated_at': now()}
        persist(item)
        return public(item)


def public(item, summary=False):
    copy = {k: v for k, v in item.items() if k not in ('media_file', 'source_url', 'revision')}
    copy['media_url'] = '/media/' + item['id'] if item.get('media_file') else None
    copy['words'] = [w for w in item['words'] if not w.get('deleted_at')]
    copy['word_count'], copy['cue_count'] = len(copy['words']), len(item['cues'])
    if summary:
        copy.pop('words', None)
        copy.pop('cues', None)
    return copy


def new_item(title, source='local', url='', media_file=''):
    return {'id': uuid.uuid4().hex, 'title': title[:250], 'created_at': now(), 'updated_at': now(),
            'source': source, 'source_url': url, 'media_file': media_file,
            'words': [], 'cues': [], 'offset': 0, 'position': 0, 'speed': 1, 'selected_word': '',
            'status': 'queued', 'error': '', 'job_stage': '已加入后台处理队列。', 'revision': 0}


def queue_item(video_id):
    with LOCK:
        item = RECORDS[video_id]
        if item['status'] in ('queued', 'downloading', 'transcribing'):
            return public(item)
        revision = item['revision'] + 1
        result = mutate(video_id, {'revision': revision, 'status': 'queued', 'error': '',
                                  'job_stage': '已加入后台处理队列。'})
        JOBS.put((video_id, revision))
        return result


def public_addresses(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise UserError('只支持不含账户密码的 HTTP / HTTPS 公网链接。')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise UserError('链接端口无效。')
    if port not in (80, 443):
        raise UserError('仅支持标准 HTTP / HTTPS 端口。')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        raise UserError('无法解析链接域名，请检查网络或更换链接。')
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise UserError('不支持本机、局域网或非公网地址。')
    return parsed, addresses


def extract_url(text):
    found = re.search(r'https?://[^\s<>"\u300b]+', text)
    if not found:
        raise UserError('未找到 HTTP / HTTPS 链接，请粘贴抖音分享内容或媒体直链。')
    url = found.group(0).rstrip('.,;!?)，。；！）')
    p = urlsplit(url)
    douyin = p.hostname in ('www.douyin.com', 'douyin.com', 'v.douyin.com')
    if not douyin and Path(p.path).suffix.lower() not in EXTENSIONS:
        raise UserError('目前支持抖音分享链接，以及以 MP4 / WebM 等扩展名结尾的媒体直链。')
    public_addresses(url)
    return url, douyin


def open_public(url, referer=None):
    """Pin each connection to the checked public IP; revalidate every redirect."""
    for _ in range(6):
        p, addresses = public_addresses(url)
        port = p.port or (443 if p.scheme == 'https' else 80)
        cls = http.client.HTTPSConnection if p.scheme == 'https' else http.client.HTTPConnection
        connection = cls(p.hostname, port, timeout=45)

        def pinned_connection(address, timeout=None, source_address=None, **kwargs):
            last = None
            for family, kind, proto, _, sockaddr in addresses:
                sock = socket.socket(family, kind, proto)
                try:
                    sock.settimeout(timeout)
                    sock.connect(sockaddr)
                    return sock
                except OSError as exc:
                    last = exc
                    sock.close()
            raise last or OSError('No public address')

        connection._create_connection = pinned_connection
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
        if referer:
            headers['Referer'] = referer
        target = urlunsplit(('', '', quote(unquote(p.path or '/'), safe='/%:@!$&()*+,;=-._~'), p.query, ''))
        try:
            connection.request('GET', target, headers=headers)
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        if response.status in (301, 302, 303, 307, 308):
            destination = response.getheader('Location')
            connection.close()
            if not destination:
                raise UserError('链接重定向缺少目标地址。')
            url = urljoin(url, destination)
            continue
        if response.status != 200:
            connection.close()
            raise UserError('来源拒绝下载或链接已失效，请下载到本机后上传。')
        return connection, response, url
    raise UserError('链接重定向过多，请改用最终视频链接。')


def download(item):
    url = item['source_url']
    p = urlsplit(url)
    is_douyin = p.hostname in ('www.douyin.com', 'douyin.com', 'v.douyin.com')
    title, suffix = item['title'], Path(p.path).suffix.lower()
    if is_douyin:
        try:
            import yt_dlp
        except ImportError:
            raise UserError('尚未安装链接解析组件，请重新运行 start.bat。')
        class QuietLogger:
            def debug(self, message): pass
            def warning(self, message): pass
            def error(self, message): pass
        try:
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
                                  'noplaylist': True, 'socket_timeout': 30, 'retries': 1,
                                  'cachedir': False, 'format': 'best[ext=mp4]/best'}) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info or info.get('_type') in ('playlist', 'multi_video') or info.get('is_live'):
                raise UserError('仅支持单个公开视频，不支持合集或直播。')
            url = info.get('url')
            if not url or info.get('protocol') not in (None, 'http', 'https'):
                raise UserError('该视频没有可直接下载的媒体地址，请保存到本机后上传。')
            suffix = '.' + info.get('ext', 'mp4')
            title = info.get('title') or title
        except UserError:
            raise
        except Exception:
            raise UserError('抖音暂时无法解析：平台可能要求新 Cookie、登录或限制下载。程序不绕过这些限制。请在抖音保存可下载的视频后上传本地文件。')
    if suffix not in EXTENSIONS:
        suffix = '.mp4'
    destination = DATA / 'media' / (item['id'] + suffix)
    temporary = destination.with_suffix(destination.suffix + '.part')
    connection = None
    try:
        connection, response, _ = open_public(url, 'https://www.douyin.com/' if is_douyin else None)
        length = response.getheader('Content-Length')
        expected = int(length) if length and length.isdigit() else None
        if expected is not None and expected > MAX_BYTES:
            raise UserError('视频超过 500 MB，请裁剪后本地上传。')
        content_type = response.getheader('Content-Type', '').lower()
        if 'text/html' in content_type or 'application/json' in content_type:
            raise UserError('链接返回的是网页而非视频文件，请使用媒体直链。')
        total = 0
        with temporary.open('wb') as stream:
            while True:
                with LOCK:
                    live = RECORDS.get(item['id'])
                    if not live or live.get('deleted_at') or live['revision'] != item['revision']:
                        raise UserError('下载任务已取消。')
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise UserError('视频超过 500 MB，请裁剪后本地上传。')
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if not total or (expected is not None and total != expected):
            raise UserError('下载中断或文件为空，请重试。')
        os.replace(temporary, destination)
        return destination.name, title[:250]
    finally:
        if connection:
            connection.close()
        temporary.unlink(missing_ok=True)


def worker():
    global MODEL
    while True:
        video_id, revision = JOBS.get()
        segments = None
        try:
            with LOCK:
                live = RECORDS.get(video_id)
                if not live or live.get('deleted_at') or live['revision'] != revision:
                    continue
                item = dict(live)
                ACTIVE_JOBS.add(video_id)
            if not item['media_file']:
                if not mutate(video_id, {'status': 'downloading', 'job_stage': '正在尝试下载视频。'}, revision):
                    continue
                filename, title = download(item)
                with LOCK:
                    if not mutate(video_id, {'media_file': filename, 'title': title}, revision):
                        (DATA / 'media' / filename).unlink(missing_ok=True)
                        continue
                item['media_file'] = filename
            if not mutate(video_id, {'status': 'transcribing', 'job_stage': '正在加载本地模型并识别；首次需下载模型。'}, revision):
                continue
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise UserError('视频已保存，但缺少语音识别组件。请运行 start.bat 安装，然后点击重新识别字幕；也可导入 SRT / VTT。')
            if MODEL is None:
                try:
                    MODEL = WhisperModel(os.environ.get('LISTEN_LAB_MODEL', 'base'), device='cpu', compute_type='int8')
                except Exception:
                    raise UserError('视频已保存，但语音模型下载或加载失败。请检查到 Hugging Face 的网络连接，或配置本地模型目录，然后重试；也可导入字幕。')
            segments, info = MODEL.transcribe(str(DATA / 'media' / item['media_file']), beam_size=5, vad_filter=True)
            cues = []
            for s in segments:
                with LOCK:
                    live = RECORDS.get(video_id)
                    if not live or live.get('deleted_at') or live['revision'] != revision:
                        break
                if s.text.strip():
                    cues.append({'start': float(s.start), 'end': float(s.end), 'text': s.text.strip()})
            mutate(video_id, {'cues': cues, 'status': 'ready', 'error': '', 'job_stage': '',
                              'language': getattr(info, 'language', '')}, revision)
        except Exception as exc:
            message = str(exc) if isinstance(exc, UserError) else '视频或链接记录已保存，但下载、模型加载或语音解码失败。请检查网络，重试或导入字幕。'
            print('Job failed:', video_id, type(exc).__name__, flush=True)
            mutate(video_id, {'status': 'error', 'error': message, 'job_stage': ''}, revision)
        finally:
            if hasattr(segments, 'close'):
                segments.close()
            with LOCK:
                ACTIVE_JOBS.discard(video_id)
            cleanup_trash()
            JOBS.task_done()


def expired(deleted_at, clock):
    return bool(deleted_at) and clock >= datetime.fromisoformat(deleted_at) + RETENTION


def purge_video(item):
    """Keep a durable tombstone until all owned files are actually removed."""
    if not item.get('purge_requested'):
        item = {**item, 'purge_requested': True}
        persist(item)
    if item['id'] in ACTIVE_JOBS:
        return False
    try:
        for path in (DATA / 'media').glob(item['id'] + '.*'):
            path.unlink(missing_ok=True)
        (DATA / 'records' / (item['id'] + '.json')).unlink(missing_ok=True)
        RECORDS.pop(item['id'], None)
        return True
    except OSError:
        return False


def cleanup_trash(clock=None):
    clock = clock or datetime.now(timezone.utc)
    with LOCK:
        for item in list(RECORDS.values()):
            try:
                if item.get('deleted_at'):
                    if item.get('purge_requested') or expired(item['deleted_at'], clock):
                        purge_video(item)
                    continue  # A deleted video's children share its retention window.
                words = [w for w in item['words'] if not expired(w.get('deleted_at'), clock)]
                if len(words) != len(item['words']):
                    persist({**item, 'words': words})
            except OSError:
                print('Trash cleanup deferred:', item['id'], flush=True)


def cleanup_loop():
    while True:
        threading.Event().wait(3600)
        cleanup_trash()


def trash_listing():
    items = []
    clock = datetime.now(timezone.utc)
    for item in RECORDS.values():
        entries = ([('video', item)] if item.get('deleted_at') else
                   [('word', w) for w in item['words'] if w.get('deleted_at')])
        for kind, entry in entries:
            deadline = datetime.fromisoformat(entry['deleted_at']) + RETENTION
            items.append({'kind': kind, 'id': entry['id'], 'video_id': item['id'],
                          'title': item['title'] if kind == 'video' else entry['query'],
                          'video_title': item['title'], 'deleted_at': entry['deleted_at'],
                          'days_left': max(0, math.ceil((deadline - clock).total_seconds() / 86400)),
                          'purge_requested': bool(entry.get('purge_requested'))})
    return sorted(items, key=lambda x: x['deleted_at'], reverse=True)


def trash_action(video_id, word_id, action):
    with LOCK:
        item = RECORDS.get(video_id)
        if not item:
            raise UserError('视频记录不存在。')
        if word_id:
            if item.get('deleted_at'):
                raise UserError('请先从回收站恢复视频。')
            word = next((w for w in item['words'] if w['id'] == word_id), None)
            if not word:
                raise UserError('生词记录不存在。')
            words = [dict(w) for w in item['words']]
            target = next(w for w in words if w['id'] == word_id)
            if action == 'trash':
                target.setdefault('deleted_at', now())
            elif action == 'restore':
                target.pop('deleted_at', None)
            elif not target.get('deleted_at'):
                raise UserError('请先将生词移入回收站。')
            else:
                words.remove(target)
            selected = item['selected_word']
            if action != 'restore' and word_key(selected) == word_key(word['query']):
                selected = ''
            persist({**item, 'words': words, 'selected_word': selected, 'updated_at': now()})
        else:
            if action == 'trash':
                if not item.get('deleted_at'):
                    changes = {'deleted_at': now(), 'revision': item['revision'] + 1}
                    if item['status'] in ('queued', 'downloading', 'transcribing'):
                        changes.update(status='error', error='处理已中断，恢复后可重新识别或重试链接导入。', job_stage='')
                    persist({**item, **changes})
            elif action == 'restore':
                if item.get('purge_requested'):
                    raise UserError('此记录已开始永久清理，不能恢复。')
                restored = {**item, 'revision': item['revision'] + 1}
                restored.pop('deleted_at', None)
                persist(restored)
            else:
                if not item.get('deleted_at'):
                    raise UserError('请先将视频移入回收站。')
                return {'pending': not purge_video(item)}
        return {'pending': False}


def valid_number(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if args and str(args[0]).startswith(('GET /api/library', 'GET /api/videos')):
            return
        super().log_message(format, *args)

    def allowed(self, mutation=False):
        host = self.headers.get('Host')
        return (host in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')
                and self.headers.get('Sec-Fetch-Site') != 'cross-site'
                and (not mutation or self.headers.get('Origin') == 'http://' + host))

    def reply(self, code, data, extra=None):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {'error': '仅允许本机页面访问。'})
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.reply(200, {'transcription_available': importlib.util.find_spec('faster_whisper') is not None,
                                    'link_import_available': importlib.util.find_spec('yt_dlp') is not None,
                                    'model': os.environ.get('LISTEN_LAB_MODEL', 'base')})
        if path == '/api/library':
            with LOCK:
                items = [public(v, True) for v in sorted(RECORDS.values(), key=lambda v: v['created_at'], reverse=True) if not v.get('deleted_at')]
            return self.reply(200, {'items': items})
        if path == '/api/trash':
            with LOCK:
                return self.reply(200, {'items': trash_listing()})
        found = re.fullmatch(r'/api/videos/(' + ID_RE + ')', path)
        if found:
            with LOCK:
                item = RECORDS.get(found[1])
                return self.reply(200, public(item)) if item and not item.get('deleted_at') else self.reply(404, {'error': '视频不存在或已移入回收站。'})
        found = re.fullmatch(r'/media/(' + ID_RE + ')', path)
        if found:
            with LOCK:
                item = RECORDS.get(found[1])
                filename = item.get('media_file') if item and not item.get('deleted_at') else None
            if filename:
                return self.send_file(DATA / 'media' / filename, media=True)
            return self.reply(404, {'error': '视频尚未下载成功。'})
        static = {'/': 'index.html', '/index.html': 'index.html', '/style.css': 'style.css',
                  '/app.js': 'app.js', '/core.js': 'core.js'}
        if path in static:
            return self.send_file(ROOT / static[path])
        self.reply(404, {'error': '未找到内容。'})

    def send_file(self, path, media=False):
        if not path.is_file():
            return self.reply(404, {'error': '文件不存在，请从备份恢复。'})
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        requested = self.headers.get('Range') if media else None
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or not (match[1] or match[2]):
                return self.reply(416, {'error': '无效范围。'}, {'Content-Range': f'bytes */{size}'})
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size - 1) if match[2] else size - 1
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                return self.reply(416, {'error': '范围超出文件。'}, {'Content-Range': f'bytes */{size}'})
            code = 206
        self.send_response(code)
        self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if media:
            self.send_header('Accept-Ranges', 'bytes')
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        if not self.allowed(True):
            return self.reply(403, {'error': '只允许从本地学习库页面操作。'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= (MAX_BYTES if self.path == '/api/upload' else 8 * 1024 * 1024):
                return self.reply(413, {'error': '文件超过 500 MB，或提交的字幕内容过大。'})
            self.connection.settimeout(120)
            if self.path == '/api/upload':
                return self.upload(length)
            raw = self.rfile.read(length)
            data = json.loads(raw or b'{}')
            if not isinstance(data, dict):
                raise UserError('请求格式无效。')
            trash_match = re.fullmatch(r'/api/videos/(' + ID_RE + r')(?:/words/(' + ID_RE + r'))?/(trash|restore|purge)', self.path)
            if trash_match:
                result = trash_action(*trash_match.groups())
                return self.reply(202 if result['pending'] else 200, result)
            if self.path == '/api/import':
                text = data.get('url', '')
                if not isinstance(text, str) or len(text) > 10000:
                    raise UserError('链接过长或格式无效。')
                url, douyin = extract_url(text)
                p = urlsplit(url)
                title = '抖音视频 · 等待导入' if douyin else unquote(Path(p.path).name)
                item = new_item(title, 'douyin' if douyin else 'link', url)
                with LOCK:
                    persist(item)
                    JOBS.put((item['id'], item['revision']))
                return self.reply(201, public(item))
            match = re.fullmatch(r'/api/videos/(' + ID_RE + r')/(words|settings|cues|clip|transcribe)', self.path)
            if not match:
                return self.reply(404, {'error': '未知操作。'})
            video_id, action = match.groups()
            with LOCK:
                if video_id not in RECORDS or RECORDS[video_id].get('deleted_at'):
                    return self.reply(404, {'error': '视频记录不存在。'})
                item = RECORDS[video_id]
                if action == 'words':
                    word = data.get('query')
                    if not isinstance(word, str) or not 1 <= len(word.strip()) <= 120:
                        raise UserError('请输入 1–120 个字符的单词或短语。')
                    word = ' '.join(word.split())
                    key = word_key
                    if not any(key(w['query']) == key(word) for w in item['words']):
                        words = item['words'] + [{'id': uuid.uuid4().hex, 'query': word, 'created_at': now()}]
                        result = mutate(video_id, {'words': words, 'selected_word': word})
                    else:
                        words = [({k: v for k, v in w.items() if k != 'deleted_at'} if key(w['query']) == key(word) else w) for w in item['words']]
                        result = mutate(video_id, {'selected_word': word, 'words': words})
                elif action == 'clip':
                    index = data.get('index')
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(item['cues']):
                        raise UserError('片段不存在，请刷新后重试。')
                    cue = item['cues'][index]
                    if data.get('original') != {k: cue[k] for k in ('start', 'end', 'text')}:
                        raise UserError('字幕已更新，请刷新后重新调整片段。')
                    updated = dict(cue)
                    if data.get('reset') is True:
                        updated.pop('clip_start', None)
                        updated.pop('clip_end', None)
                    else:
                        start, end, duration = data.get('start'), data.get('end'), data.get('duration')
                        if (not valid_number(duration, 0.05, 604800) or not valid_number(start, 0, duration)
                                or not valid_number(end, 0, duration) or end - start < 0.049999):
                            raise UserError('起点必须早于终点，片段至少 0.05 秒，且不能超出视频时长。')
                        updated.update(clip_start=start, clip_end=end)
                    cues = list(item['cues'])
                    cues[index] = updated
                    result = mutate(video_id, {'cues': cues})
                elif action == 'settings':
                    bounds = {'offset': (-3600, 3600), 'position': (0, 604800), 'speed': (0.25, 2)}
                    changes = {}
                    for name, value in data.items():
                        if name == 'selected_word' and isinstance(value, str) and len(value) <= 120:
                            changes[name] = value if any(word_key(w['query']) == word_key(value) and not w.get('deleted_at') for w in item['words']) else ''
                            continue
                        if name not in bounds or not valid_number(value, *bounds[name]):
                            raise UserError('播放设置无效。')
                        changes[name] = value
                    result = mutate(video_id, changes)
                elif action == 'cues':
                    cues = data.get('cues')
                    if not isinstance(cues, list) or len(cues) > 50000:
                        raise UserError('字幕格式无效或片段过多。')
                    for cue in cues:
                        if (not isinstance(cue, dict) or not valid_number(cue.get('start'), 0, 604800)
                                or not valid_number(cue.get('end'), 0, 604800) or cue['end'] <= cue['start']
                                or not isinstance(cue.get('text'), str) or not 1 <= len(cue['text']) <= 5000):
                            raise UserError('字幕内容或时间轴无效。')
                    cues = sorted([{'start': c['start'], 'end': c['end'], 'text': c['text']} for c in cues], key=lambda c: c['start'])
                    result = mutate(video_id, {'cues': cues, 'revision': item['revision'] + 1,
                                              'status': 'ready', 'error': '', 'job_stage': ''})
                else:
                    result = queue_item(video_id)
            self.reply(200, result)
        except (UserError, ValueError, UnicodeError) as exc:
            self.reply(400, {'error': str(exc) if isinstance(exc, UserError) else '请求内容或格式无效。'})
        except OSError:
            self.reply(500, {'error': '本地读写失败，请检查磁盘空间或文件夹权限；此次修改可能未保存。'})

    def upload(self, length):
        if not length:
            raise UserError('文件为空。')
        filename = unquote(self.headers.get('X-Filename', 'video.mp4')).replace('\\', '/').split('/')[-1]
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTENSIONS:
            raise UserError('请选择 MP4 / WebM 等视频或音频文件。')
        item = new_item(filename)
        item['media_file'] = item['id'] + suffix
        destination = DATA / 'media' / item['media_file']
        temporary = destination.with_suffix(suffix + '.part')
        try:
            with temporary.open('wb') as stream:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise UserError('上传中断，请重试。')
                    stream.write(chunk)
                    remaining -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            with LOCK:
                persist(item)
                JOBS.put((item['id'], item['revision']))
            return self.reply(201, public(item))
        finally:
            temporary.unlink(missing_ok=True)


def initialize():
    for sub in ('records', 'media'):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    # A corrupt record must be reported, not silently discarded.
    for path in (DATA / 'records').glob('*.json'):
        item = json.loads(path.read_text(encoding='utf-8'))
        if item['status'] in ('queued', 'downloading', 'transcribing'):
            item.update(status='error', error='上次处理被中断，已保存的内容仍在。请点击重新识别字幕重试。', job_stage='')
            item['revision'] += 1
            persist(item)
        RECORDS[item['id']] = item
    cleanup_trash()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    DATA.mkdir(parents=True, exist_ok=True)
    instance_lock = (DATA / '.instance.lock').open('a+b')
    instance_lock.seek(0, 2)
    if instance_lock.tell() == 0:
        instance_lock.write(b'0')
        instance_lock.flush()
    instance_lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        server.server_close()
        raise SystemExit('This library is already open in another Listen Lab server. Close it first.')
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f'Listen Lab: http://127.0.0.1:{PORT}', flush=True)
    print(f'Your library: {DATA}', flush=True)
    print('Keep this window running. Ctrl+C stops the app.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        instance_lock.close()
