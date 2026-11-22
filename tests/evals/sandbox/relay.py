"""Inside the agent container: 127.0.0.1:8787 -> the proxy's Unix socket. Holds no secret."""
import socket
import socketserver
import threading

PORT = 8787
SOCKET = "/run/proxy/api.sock"


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as out:
            out.connect(SOCKET)
            back = threading.Thread(target=pump, args=(out, self.request), daemon=True)
            back.start()
            pump(self.request, out)
            back.join()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("127.0.0.1", PORT), Relay) as server:
        print("READY", flush=True)
        server.serve_forever()
