#!/usr/bin/env python3
"""boxportal: serves the EmergencyBox captive message (capture-wsgi.py) on 127.0.0.1:8183 (BOXPORTAL_PORT).
nginx sends the phones' captive checks here. Python standard library only."""
import importlib.util, os, sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("capture", os.path.join(HERE, "capture-wsgi.py"))
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass


port = int(os.environ.get("BOXPORTAL_PORT", "8183"))
sys.stderr.write("boxportal on 127.0.0.1:%d, state in %s\n" % (port, capture.DB))
make_server("127.0.0.1", port, capture.application, Server, Quiet).serve_forever()
