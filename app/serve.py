"""Serve the PWA with versioned, browser-cacheable local code assets.

The HTML is revalidated on entry. Script and style URLs use their file mtime as
a version, so editing a file changes its URL on the next refresh without a build.
"""
import io
import os
import re
from http.server import SimpleHTTPRequestHandler, test
from urllib.parse import parse_qs, urlsplit


APP_DIR = os.path.dirname(__file__)
ASSET_REF = re.compile(r'(?P<prefix>\b(?:src|href)=")(?P<path>(?:src|styles)/[^"?]+)(?P<suffix>")')


def version_asset(match):
    path = match.group('path')
    version = os.stat(os.path.join(APP_DIR, path)).st_mtime_ns
    return '{}{}?v={}{}'.format(match.group('prefix'), path, version, match.group('suffix'))


class PwaHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        if urlsplit(self.path).path in ('/', '/index.html'):
            with open(os.path.join(APP_DIR, 'index.html'), encoding='utf-8') as source:
                html = ASSET_REF.sub(version_asset, source.read()).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            return io.BytesIO(html)
        return super().send_head()

    def end_headers(self):
        url = urlsplit(self.path)
        path = url.path.lstrip('/')
        version = parse_qs(url.query).get('v', [None])[0]
        is_code_asset = path.startswith(('src/', 'styles/'))
        if is_code_asset and version:
            try:
                current_version = str(os.stat(os.path.join(APP_DIR, path)).st_mtime_ns)
            except OSError:
                current_version = None
            if version == current_version:
                self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
            else:
                self.send_header('Cache-Control', 'no-cache, must-revalidate')
        else:
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # Avoid noisy static-file logs.


if __name__ == '__main__':
    os.chdir(APP_DIR)
    test(HandlerClass=PwaHandler, port=8081, bind='127.0.0.1')
