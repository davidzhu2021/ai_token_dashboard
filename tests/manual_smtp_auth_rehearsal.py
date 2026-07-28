"""Rehearse the live --auth-only path against a local AUTH-requiring SMTP sink.

This proves the exact code path the real 263 run will take (implicit TLS on
465, AUTH LOGIN, single verification email) without needing real credentials.
The only untested variable afterwards is whether the 263 account accepts the
username/password.

    python -m tests.manual_smtp_auth_rehearsal
"""

import base64
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

EXPECTED_USERNAME = "rehearsal-user@auto-link.com.cn"
EXPECTED_PASSWORD = "rehearsal-password"


def _self_signed_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "rehearsal-cert.pem"
    key_path = directory / "rehearsal-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class AuthRequiringSMTPSSLServer:
    """Implicit-TLS SMTP sink that rejects delivery unless AUTH LOGIN succeeds."""

    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self.messages: list[str] = []
        self.authenticated = False
        self.auth_attempts: list[str] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        self._context = context
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.host, self.port = self._socket.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "AuthRequiringSMTPSSLServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _address = self._socket.accept()
            except OSError:
                return
            try:
                with self._context.wrap_socket(raw, server_side=True) as connection:
                    self._handle(connection)
            except (OSError, ssl.SSLError):
                pass

    def _handle(self, connection: ssl.SSLSocket) -> None:
        stream = connection.makefile("rwb", buffering=0)
        stream.write(b"220 rehearsal.local ESMTP\r\n")
        state = {"authed": False, "expect": ""}
        while True:
            line = stream.readline()
            if not line:
                return
            raw_command = line.decode("utf-8", "replace").strip()
            upper = raw_command.upper()
            if state["expect"] == "username":
                self.auth_attempts.append(base64.b64decode(raw_command).decode("utf-8", "replace"))
                state["expect"] = "password"
                stream.write(b"334 UGFzc3dvcmQ6\r\n")
            elif state["expect"] == "password":
                password = base64.b64decode(raw_command).decode("utf-8", "replace")
                state["expect"] = ""
                if self.auth_attempts[-1] == EXPECTED_USERNAME and password == EXPECTED_PASSWORD:
                    state["authed"] = True
                    self.authenticated = True
                    stream.write(b"235 2.7.0 Authentication successful\r\n")
                else:
                    stream.write(b"535 5.7.8 Authentication credentials invalid\r\n")
            elif upper.startswith("EHLO") or upper.startswith("HELO"):
                stream.write(b"250-rehearsal.local\r\n250-AUTH LOGIN PLAIN\r\n250 SIZE 10485760\r\n")
            elif upper.startswith("AUTH LOGIN"):
                # smtplib sends the username inline as an initial response
                # ("AUTH LOGIN <base64user>"), so only prompt when it is absent.
                inline = raw_command[len("AUTH LOGIN") :].strip()
                if inline:
                    self.auth_attempts.append(base64.b64decode(inline).decode("utf-8", "replace"))
                    state["expect"] = "password"
                    stream.write(b"334 UGFzc3dvcmQ6\r\n")
                else:
                    state["expect"] = "username"
                    stream.write(b"334 VXNlcm5hbWU6\r\n")
            elif upper.startswith("MAIL FROM"):
                if not state["authed"]:
                    stream.write(b"530 5.7.0 Authentication required\r\n")
                else:
                    stream.write(b"250 OK\r\n")
            elif upper.startswith("RCPT TO"):
                stream.write(b"250 OK\r\n" if state["authed"] else b"530 5.7.0 Authentication required\r\n")
            elif upper == "DATA":
                if not state["authed"]:
                    stream.write(b"530 5.7.0 Authentication required\r\n")
                    continue
                stream.write(b"354 End data\r\n")
                chunks: list[str] = []
                while True:
                    data_line = stream.readline()
                    if not data_line or data_line in (b".\r\n", b".\n"):
                        break
                    chunks.append(data_line.decode("utf-8", "replace"))
                self.messages.append("".join(chunks))
                stream.write(b"250 Queued\r\n")
            elif upper == "QUIT":
                stream.write(b"221 Bye\r\n")
                return
            else:
                stream.write(b"250 OK\r\n")


def _run_harness(server, *, password: str, cert_path: Path) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.update(
        {
            "SMTP_HOST": "localhost",
            "SMTP_PORT": str(server.port),
            "SMTP_SSL": "true",
            "SMTP_STARTTLS": "false",
            "SMTP_FROM": "no-reply@auto-link.com.cn",
            "SMTP_USERNAME": EXPECTED_USERNAME,
            "SMTP_PASSWORD": password,
            # Trust the rehearsal certificate so create_default_context() verifies.
            "SSL_CERT_FILE": str(cert_path),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.manual_smtp_delivery_check",
            "live",
            "--auth-only",
            "--recipient",
            "rehearsal-recipient@gmail.com",
            "--database",
            ".data/manual-smtp-rehearsal.sqlite3",
        ],
        cwd=str(ROOT_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cert_path, key_path = _self_signed_cert(Path(directory))

        print("=== case 1: wrong password must fail loudly ===")
        server = AuthRequiringSMTPSSLServer(cert_path, key_path).start()
        try:
            result = _run_harness(server, password="wrong-password", cert_path=cert_path)
            print((result.stdout or "").strip()[-700:])
            if result.returncode == 0:
                raise SystemExit("FAIL bad credentials were reported as success")
            if server.messages:
                raise SystemExit("FAIL unauthenticated message was accepted")
            print(f"  ok   harness failed as expected (exit {result.returncode}), 0 messages delivered")
        finally:
            server.stop()

        print("\n=== case 2: correct password must authenticate and deliver ===")
        server = AuthRequiringSMTPSSLServer(cert_path, key_path).start()
        try:
            result = _run_harness(server, password=EXPECTED_PASSWORD, cert_path=cert_path)
            print((result.stdout or "").strip()[-900:])
            if result.returncode != 0:
                print((result.stderr or "").strip()[-700:])
                raise SystemExit(f"FAIL harness exited {result.returncode}")
            if not server.authenticated:
                raise SystemExit("FAIL server never saw a successful AUTH LOGIN")
            if len(server.messages) != 1:
                raise SystemExit(f"FAIL expected exactly 1 delivered message, got {len(server.messages)}")
            print(f"  ok   AUTH LOGIN username seen = {server.auth_attempts[-1]!r}")
            print(f"  ok   messages delivered = {len(server.messages)}")
        finally:
            server.stop()

    Path(ROOT_DIR / ".data" / "manual-smtp-rehearsal.sqlite3").unlink(missing_ok=True)
    print("\nPASS rehearsal: implicit TLS + AUTH LOGIN + single-email path verified.")
    print("     Only the real 263 username/password remains untested.")


if __name__ == "__main__":
    main()
