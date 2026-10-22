import http.client
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from tokenometer.server import make_server

class ServerTests(unittest.TestCase):
    def test_loopback_allowlist_blocks_private_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'web';root.mkdir();(root/'index.html').write_text('<h1>Public</h1>');(root/'data.json').write_text('{}')
            secret=Path(tmp)/'secret';secret.write_text('PRIVATE_CANARY');(root/'app.js').symlink_to(secret)
            (root/'.private').write_text('PRIVATE_CANARY');(root/'other.json').write_text('PRIVATE_CANARY')
            server=make_server(root,0);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                self.assertEqual(server.server_address[0],'127.0.0.1')
                for route,expected in [('/',200),('/data.json',200),('/app.js',404),('/.private',404),('/other.json',404),
                        ('/../secret',404),('/%2e%2e/secret',404),('/.local/ledger/usage.sqlite3',404),('/%2f..%2fsecret',404)]:
                    client=http.client.HTTPConnection('127.0.0.1',server.server_port)
                    client.request('GET',route);response=client.getresponse();body=response.read()
                    self.assertEqual(response.status,expected,route);self.assertNotIn(b'PRIVATE_CANARY',body)
                    self.assertIn("default-src 'self'",response.getheader('Content-Security-Policy'))
                    client.close()
            finally:server.shutdown();server.server_close();thread.join()

    def test_symlinked_root_cannot_turn_private_directory_into_webroot(self):
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)/'private';private.mkdir();(private/'data.json').write_text('PRIVATE_CANARY')
            root=Path(tmp)/'web';root.symlink_to(private,target_is_directory=True)
            with self.assertRaises(ValueError):make_server(root,0)
