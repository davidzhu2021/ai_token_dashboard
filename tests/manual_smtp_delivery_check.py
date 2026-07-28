"""Manual end-to-end check that registration email actually leaves over SMTP.

This is a manual verification harness, not part of the pytest suite. Unlike
`tests/test_local_auth_routes.py` it does NOT stub `send_auth_email`, so the
real `smtplib` transport in `backend.main.send_auth_email_sync` is exercised.

Two stages:

  capture  Run a local capture SMTP server and drive the full HTTP flow
           (verification code -> register -> login). Proves the transport and
           the route wiring without needing provider credentials.

  live     Point the same flow at a real SMTP provider (263) and deliver a
           real verification code to a real mailbox.

Usage:

    # Stage A - no credentials needed
    python -m tests.manual_smtp_delivery_check capture

    # Stage B - real delivery. Credentials come from the environment only;
    # never pass them on the command line and never commit them.
    export SMTP_HOST=smtp.263.net SMTP_PORT=465
    export SMTP_SSL=true SMTP_STARTTLS=false
    export SMTP_FROM=no-reply@auto-link.com.cn
    export SMTP_USERNAME=... SMTP_PASSWORD=...
    python -m tests.manual_smtp_delivery_check live --recipient you@auto-link.com.cn
"""

import argparse
import asyncio
import os
import re
import socket
import ssl
import sys
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from backend import main  # noqa: E402
from backend.auth_store import AuthStore  # noqa: E402


class CaptureSMTPServer:
    """Minimal plaintext SMTP sink that records the messages it receives."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self._socket.listen(5)
        self.host, self.port = self._socket.getsockname()
        self.messages: list[dict[str, str]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "CaptureSMTPServer":
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
                connection, _address = self._socket.accept()
            except OSError:
                return
            try:
                self._handle(connection)
            except OSError:
                pass
            finally:
                connection.close()

    def _handle(self, connection: socket.socket) -> None:
        stream = connection.makefile("rwb", buffering=0)
        stream.write(b"220 capture.local ESMTP\r\n")
        envelope: dict[str, str] = {"rcpt": "", "body": ""}
        while True:
            line = stream.readline()
            if not line:
                return
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                stream.write(b"250-capture.local\r\n250 SIZE 10485760\r\n")
            elif upper.startswith("MAIL FROM"):
                stream.write(b"250 OK\r\n")
            elif upper.startswith("RCPT TO"):
                envelope["rcpt"] = command.partition(":")[2].strip().strip("<>")
                stream.write(b"250 OK\r\n")
            elif upper == "DATA":
                stream.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                chunks: list[str] = []
                while True:
                    data_line = stream.readline()
                    if not data_line or data_line in (b".\r\n", b".\n"):
                        break
                    chunks.append(data_line.decode("utf-8", "replace"))
                envelope["body"] = "".join(chunks)
                self.messages.append(dict(envelope))
                stream.write(b"250 Queued\r\n")
            elif upper == "QUIT":
                stream.write(b"221 Bye\r\n")
                return
            else:
                stream.write(b"250 OK\r\n")


class RecordingProvisioningClient:
    """Stands in for the usage backend so the check never touches upstream."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_internal_user(self, user_id, email, name=None):
        self.created.append({"user_id": user_id, "email": email, "name": name})
        return {"user_id": user_id, "user_email": email}

    async def user_info(self, user_id):
        return {"user_id": user_id, "models": ["no-default-models"], "max_budget": None}

    async def usage_rows_for_user_ids(self, *_args, **_kwargs):
        return []


def _configure(recipient: str, database_path: Path, *, smtp_env: dict[str, str]) -> RecordingProvisioningClient:
    upstream = RecordingProvisioningClient()
    main._auth_store = AuthStore(database_path)
    main._litellm_client = upstream
    os.environ.update(
        {
            "AUTH_ENABLED": "true",
            "PASSWORD_LOGIN_ENABLED": "true",
            "PUBLIC_SIGNUP_ENABLED": "true",
            "EMAIL_VERIFICATION_REQUIRED": "true",
            # Loopback base URL keeps this a development-only harness and lets
            # smtp_configured() accept an anonymous capture server.
            "APP_BASE_URL": "http://127.0.0.1:8000",
            "AUTH_EMAIL_DEBUG": "true",
            "AUTH_DATABASE_PATH": str(database_path),
            "AUTH_ALLOWED_EMAIL_DOMAINS": recipient.rsplit("@", 1)[1],
            "AUTH_DEFAULT_UPSTREAM_ROLE": "internal_user_viewer",
            "TURNSTILE_ENABLED": "false",
            "TURNSTILE_SITE_KEY": "",
            "TURNSTILE_SECRET_KEY": "",
            **smtp_env,
        }
    )
    return upstream


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrfToken"]}


def _require_ok(label: str, response) -> dict:
    if response.status_code >= 400:
        raise SystemExit(f"FAIL {label}: HTTP {response.status_code} {response.text}")
    print(f"  ok   {label}: HTTP {response.status_code}")
    return response.json()


def _report_readiness(client: TestClient) -> dict:
    config = client.get("/api/auth/config").json()
    print("\n[auth config]")
    for key in (
        "passwordLoginAvailable",
        "publicSignupAvailable",
        "passwordRecoveryAvailable",
        "publicSignupUnavailableCode",
        "emailVerificationRequired",
        "allowedSignupDomains",
    ):
        print(f"  {key} = {config.get(key)!r}")
    if not config.get("publicSignupAvailable"):
        raise SystemExit(
            "FAIL signup not available: "
            f"{config.get('publicSignupUnavailableCode')} / {config.get('publicSignupUnavailableReason')}"
        )
    return config


def probe_transport(host: str, port: int, *, use_ssl: bool, starttls: bool) -> None:
    """Report the provider banner and AUTH mechanisms before sending anything."""
    print(f"\n[transport probe] {host}:{port} ssl={use_ssl} starttls={starttls}")
    import smtplib

    if use_ssl:
        connection = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(host, port, timeout=15)
    try:
        connection.ehlo()
        if starttls and not use_ssl:
            connection.starttls(context=ssl.create_default_context())
            connection.ehlo()
        print(f"  esmtp_features = {sorted(connection.esmtp_features)}")
        print(f"  auth advertised = {connection.esmtp_features.get('auth')!r}")
    finally:
        try:
            connection.quit()
        except smtplib.SMTPException:
            connection.close()


def run_flow(client: TestClient, recipient: str, *, code_source) -> None:
    print("\n[flow] request verification code")
    _require_ok(
        "POST /api/auth/verification/request",
        client.post(
            "/api/auth/verification/request",
            json={"email": recipient, "purpose": "signup", "turnstileToken": ""},
            headers=_csrf(client),
        ),
    )

    code = code_source()
    if not code:
        print("\n[flow] no code available locally; stopping before register.")
        print("       Check the recipient mailbox to confirm delivery and content.")
        return
    print(f"  captured verification code = {code}")

    print("\n[flow] register")
    payload = _require_ok(
        "POST /api/auth/register",
        client.post(
            "/api/auth/register",
            json={
                "email": recipient,
                "name": "SMTP Delivery Check",
                "password": "Smtp-Check-Passw0rd!",
                "verificationCode": code,
                "turnstileToken": "",
            },
            headers=_csrf(client),
        ),
    )
    user = payload.get("user") or {}
    print(f"  accountStatus     = {user.get('accountStatus')!r}")
    print(f"  entitlementStatus = {user.get('entitlementStatus')!r}")

    print("\n[flow] login")
    login = _require_ok(
        "POST /api/auth/login",
        client.post(
            "/api/auth/login",
            json={"email": recipient, "password": "Smtp-Check-Passw0rd!", "turnstileToken": ""},
            headers=_csrf(client),
        ),
    )
    print(f"  session user = {(login.get('user') or {}).get('email')!r}")

    print("\n[flow] forgot password (second real email)")
    _require_ok(
        "POST /api/auth/password/forgot",
        client.post(
            "/api/auth/password/forgot",
            json={"email": recipient, "turnstileToken": ""},
            headers=_csrf(client),
        ),
    )


def stage_capture(args) -> None:
    server = CaptureSMTPServer().start()
    print(f"[capture] SMTP sink listening on {server.host}:{server.port}")
    try:
        _configure(
            args.recipient,
            Path(args.database),
            smtp_env={
                "SMTP_HOST": server.host,
                "SMTP_PORT": str(server.port),
                "SMTP_FROM": "no-reply@auto-link.com.cn",
                "SMTP_USERNAME": "",
                "SMTP_PASSWORD": "",
                "SMTP_SSL": "false",
                "SMTP_STARTTLS": "false",
            },
        )
        client = TestClient(main.app)
        _report_readiness(client)

        def code_from_capture() -> str:
            if not server.messages:
                raise SystemExit("FAIL no message reached the capture SMTP server")
            body = server.messages[-1]["body"]
            match = re.search(r"(\d{6})", body)
            return match.group(1) if match else ""

        run_flow(client, args.recipient, code_source=code_from_capture)

        print(f"\n[capture] messages received over real SMTP: {len(server.messages)}")
        for index, message in enumerate(server.messages, start=1):
            subject = re.search(r"^Subject:\s*(.+)$", message["body"], re.MULTILINE)
            print(f"  {index}. rcpt={message['rcpt']} subject={subject.group(1).strip() if subject else '?'}")
        if len(server.messages) < 2:
            raise SystemExit("FAIL expected both a verification email and a reset email")
    finally:
        server.stop()
    print("\nPASS capture stage: real smtplib transport delivered both emails.")


def stage_live(args) -> None:
    missing = [name for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD") if not os.getenv(name)]
    if missing:
        raise SystemExit(
            "FAIL live stage needs real provider credentials in the environment: "
            + ", ".join(missing)
            + "\nSet them in the shell (not on the command line, not in git) and retry."
        )
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    use_ssl = os.getenv("SMTP_SSL", "true").lower() in {"1", "true", "yes", "on"}
    starttls = os.getenv("SMTP_STARTTLS", "false").lower() in {"1", "true", "yes", "on"}
    probe_transport(host, port, use_ssl=use_ssl, starttls=starttls)

    _configure(
        args.recipient,
        Path(args.database),
        smtp_env={
            "SMTP_HOST": host,
            "SMTP_PORT": str(port),
            "SMTP_FROM": os.environ["SMTP_FROM"],
            "SMTP_USERNAME": os.environ["SMTP_USERNAME"],
            "SMTP_PASSWORD": os.environ["SMTP_PASSWORD"],
            "SMTP_SSL": "true" if use_ssl else "false",
            "SMTP_STARTTLS": "true" if starttls else "false",
        },
    )
    client = TestClient(main.app)
    _report_readiness(client)

    if args.auth_only:
        print("\n[live] auth-only mode: SMTP login verified above; sending one real email.")
        _require_ok(
            "POST /api/auth/verification/request",
            client.post(
                "/api/auth/verification/request",
                json={"email": args.recipient, "purpose": "signup", "turnstileToken": ""},
                headers=_csrf(client),
            ),
        )
        print(f"\nDONE one verification email accepted by {host} for {args.recipient}.")
        print("     Now check that mailbox and report:")
        print("       1. inbox or spam")
        print("       2. Received-SPF / DKIM / DMARC results in the raw headers")
        print("     Then rotate the temporary SMTP password.")
        return

    def code_from_operator() -> str:
        if args.code:
            return args.code
        print("\n[live] verification email accepted by the provider.")
        print(f"       Open the mailbox for {args.recipient} and read the 6-digit code.")
        print("       Then rerun with --code <code> to finish register + login,")
        print("       or press Enter here to continue interactively.")
        try:
            entered = input("       code (blank to stop): ").strip()
        except EOFError:
            entered = ""
        captured["code"] = entered
        return entered

    run_flow(client, args.recipient, code_source=code_from_operator)
    print("\nDONE live stage. Confirm the message landed in the inbox, not spam,")
    print("     and check the received headers for SPF/DKIM/DMARC results.")


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["capture", "live"])
    parser.add_argument("--recipient", default="smtp-check@auto-link.com.cn")
    parser.add_argument("--database", default=".data/manual-smtp-check.sqlite3")
    parser.add_argument("--code", default="", help="live stage only: verification code read from the mailbox")
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="live stage only: verify SMTP auth and send one verification email, then stop",
    )
    args = parser.parse_args()
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    if Path(args.database).exists():
        Path(args.database).unlink()
    if args.stage == "capture":
        stage_capture(args)
    else:
        stage_live(args)


if __name__ == "__main__":
    main_cli()
