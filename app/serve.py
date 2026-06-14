"""PWA 静态文件服务器，发 Cache-Control: no-cache 让浏览器每次重验证。

效果：改了 src/ 下任何文件，刷新浏览器即生效，无需手动改版本号。
"""
import os
from http.server import SimpleHTTPRequestHandler, test


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏


if __name__ == '__main__':
    os.chdir(os.path.dirname(__file__))
    test(HandlerClass=NoCacheHandler, port=8081, bind='127.0.0.1')
