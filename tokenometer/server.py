"""Loopback-only static preview, explicitly limited to public authored assets."""
from __future__ import annotations
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ALLOWED = frozenset(('/', '/index.html','/styles.css','/app.js','/data.json','/favicon.svg'))


class PublicHandler(SimpleHTTPRequestHandler):
    def _allowed(self):
        path=unquote(urlsplit(self.path).path)
        if path not in ALLOWED: return False
        target=Path(self.directory)/(path.lstrip('/') or 'index.html')
        root=Path(self.directory).resolve()
        try: target.resolve().relative_to(root)
        except (OSError,ValueError): return False
        return target.is_file() and not target.is_symlink()

    def do_GET(self):
        if not self._allowed(): self.send_error(404);return
        super().do_GET()

    def do_HEAD(self):
        if not self._allowed(): self.send_error(404);return
        super().do_HEAD()

    def end_headers(self):
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def log_message(self,format,*args):
        # No request paths/queries in local logs.
        pass


def make_server(root:Path,port:int=4317):
    root=Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Preview requires a real public web directory, not a symlink')
    return ThreadingHTTPServer(('127.0.0.1',port),partial(PublicHandler,directory=str(Path(root).resolve())))


def serve(root:Path,port:int=4317):
    server=make_server(root,port)
    print(f'Local preview: http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
