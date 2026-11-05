"""Container health check that speaks the *uwsgi protocol* directly.

The web container only exposes a uwsgi socket (no HTTP), so curl can't probe
it. A uwsgi request packet is tiny:

    header: modifier1 (u8) | datasize (u16 little-endian) | modifier2 (u8)
    body:   repeated  key_len (u16 LE) | key | value_len (u16 LE) | value

i.e. the WSGI environ serialised as key/value pairs. We send GET /healthz and
expect an HTTP 200 status line back.
"""

import socket
import struct
import sys


def uwsgi_packet(env: dict[str, str]) -> bytes:
    body = b"".join(
        struct.pack("<H", len(k)) + k + struct.pack("<H", len(v)) + v
        for k, v in ((k.encode(), v.encode()) for k, v in env.items())
    )
    return struct.pack("<BHB", 0, len(body), 0) + body


def main(host: str = "127.0.0.1", port: int = 8000) -> int:
    env = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/healthz",
        "REQUEST_URI": "/healthz",
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": str(port),
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_HOST": "localhost",
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.url_scheme": "http",
    }
    with socket.create_connection((host, port), timeout=3) as conn:
        conn.sendall(uwsgi_packet(env))
        status_line = conn.recv(64).split(b"\r\n", 1)[0]
    ok = status_line.split()[1:2] == [b"200"]
    print(status_line.decode(errors="replace"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
