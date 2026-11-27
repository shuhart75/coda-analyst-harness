"""Nonblocking availability checks for explicitly recorded local project URLs."""
from http.client import HTTPException
import threading
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit


def project_urls(records):
    # A browser checklist identifies a UI; a database port or health API does not.
    result = {}
    for record in sorted(records, key=lambda r: r.get('created_at', 0), reverse=True):
        check = record.get('manual_check') or (record.get('automation', {}).get('verification') or {}).get('check') or {}
        url = check.get('url')
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
            if (parts.scheme not in ('http', 'https') or parts.hostname not in ('localhost', '127.0.0.1', '::1')
                    or parts.username or parts.password or any(ord(c) < 32 for c in url)):
                continue
            origin = (parts.scheme, parts.hostname, parts.port)
        except ValueError:
            continue
        result.setdefault(origin, quote(url, safe=":/?#[]@!$&'()*+,;=%"))
        if len(result) == 8:
            break
    return list(result.values())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def reachable(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method='HEAD'), timeout=1) as response:
            code = response.status
    except HTTPError as error:
        code = error.code
    except (OSError, ValueError, HTTPException, UnicodeError):
        return False
    # Login/redirects and servers without HEAD still answer; do not follow redirects.
    return 200 <= code < 400 or code in (401, 403, 405)


class ProjectLinks:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.pending = set()
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True

    def view(self, key, records):
        urls = project_urls(records)
        now = time.time()
        with self.lock:
            result = []
            for url in urls:
                identity = (key, url)
                item = self.cache.get(identity)
                if (not item or now-item['checked_at'] >= 10) and identity not in self.pending and len(self.pending) < 8 and not self.closed:
                    self.pending.add(identity)
                    threading.Thread(target=self._check, args=(identity,), daemon=True).start()
                result.append({'url': url, 'available': bool(item and now-item['checked_at'] < 30 and item['available']),
                               'checked_at': item['checked_at'] if item else None,
                               'checking': identity in self.pending})
            self.cache = {k: v for k, v in self.cache.items() if k[0] == key and k[1] in urls}
            return result

    def _check(self, identity):
        try:
            available = reachable(identity[1])
            with self.lock:
                if not self.closed:
                    self.cache[identity] = {'available': available, 'checked_at': time.time()}
        finally:
            with self.lock:
                self.pending.discard(identity)
