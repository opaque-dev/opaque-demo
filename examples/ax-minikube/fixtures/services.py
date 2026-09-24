#!/usr/bin/env python3
"""Disposable protocol fixtures: synthetic OIDC identity and HTTPS support cases.

The identity fixture auto-authorizes fixed test accounts; it proves no human
identity. Opaque's native reviewer is a separate process outside this container.
No approval key or approval-signing endpoint exists here. Keep state outside Git.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import threading
import time
import urllib.parse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

ROOT = Path("/fixture")
ISSUER = "http://127.0.0.1:18912"
CLIENT = "opaque-live-scope-fixture"
CALLBACK = "http://127.0.0.1:18916/callback"
LOCK = threading.Lock()
CODES = {}
CASES = {}
EFFECTS = {}


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def event(value):
    # Only selected synthetic IDs/state, never authorization headers or tokens.
    with (ROOT / "provider-events.jsonl").open("a") as output:
        output.write(json.dumps(value, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def initialize():
    global SIGNER, TOKEN
    now = datetime.now(timezone.utc)
    if not (ROOT / "tls-key.pem").exists():
        tls_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic local support fixture")])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=2))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .sign(tls_key, hashes.SHA256()))
        private(ROOT / "tls-key.pem", tls_key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private(ROOT / "provider-ca.pem", cert.public_bytes(serialization.Encoding.PEM))
        private(ROOT / "provider.token", secrets.token_urlsafe(32).encode())
    TOKEN = (ROOT / "provider.token").read_text()
    key_path = ROOT / "oidc-key.pem"
    if not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        private(key_path, key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    SIGNER = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if (ROOT / "provider-events.jsonl").exists():
        for line in (ROOT / "provider-events.jsonl").read_text().splitlines():
            item = json.loads(line)
            if item["kind"] == "effect":
                CASES[item["resource"]] = item["case"]
                EFFECTS[item["action_id"]] = item
    else:
        private(ROOT / "provider-events.jsonl", b"")


def jwt(claims):
    head = encoded(json.dumps({"alg": "ES256", "kid": "synthetic-oidc", "typ": "JWT"}).encode())
    body = encoded(json.dumps(claims).encode())
    signing = f"{head}.{body}".encode()
    r, s = decode_dss_signature(SIGNER.sign(signing, ec.ECDSA(hashes.SHA256())))
    return f"{head}.{body}.{encoded(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 16384:
            raise ValueError("request body out of bounds")
        return self.rfile.read(length)


class Identity(QuietHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/.well-known/openid-configuration":
            self.reply(200, {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                            "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"})
        elif url.path == "/jwks":
            point = SIGNER.public_key().public_numbers()
            self.reply(200, {"keys": [{"kty": "EC", "kid": "synthetic-oidc", "alg": "ES256", "use": "sig",
                "crv": "P-256", "x": encoded(point.x.to_bytes(32, "big")), "y": encoded(point.y.to_bytes(32, "big"))}]})
        elif url.path == "/authorize":
            query = urllib.parse.parse_qs(url.query, strict_parsing=True)
            if any(len(values) != 1 for values in query.values()):
                self.reply(400, {"error": "duplicate_parameter"})
                return
            query = {key: values[0] for key, values in query.items()}
            subject = query.get("fixture_subject")
            if (subject not in {"reviewer", "requester"} or query.get("redirect_uri") != CALLBACK
                    or query.get("client_id") != CLIENT or query.get("response_type") != "code"
                    or query.get("code_challenge_method") != "S256" or not query.get("state")
                    or not query.get("nonce") or not query.get("code_challenge")):
                self.reply(400, {"error": "invalid_synthetic_authorization"})
                return
            code = secrets.token_urlsafe(32)
            with LOCK:
                CODES[code] = {**query, "expires": time.time() + 60}
            self.send_response(302)
            self.send_header("Location", CALLBACK + "?" + urllib.parse.urlencode({"state": query["state"], "code": code}))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.reply(404, {"error": "unknown_endpoint"})

    def do_POST(self):
        if self.path != "/token":
            self.reply(404, {"error": "unknown_endpoint"})
            return
        data = urllib.parse.parse_qs(self.body().decode(), strict_parsing=True)
        if any(len(value) != 1 for value in data.values()):
            self.reply(400, {"error": "invalid_grant"})
            return
        data = {key: value[0] for key, value in data.items()}
        with LOCK:
            code = CODES.pop(data.get("code", ""), None)
        if (code is None or code["expires"] < time.time() or data.get("client_id") != CLIENT
                or data.get("redirect_uri") != CALLBACK or data.get("grant_type") != "authorization_code"
                or encoded(hashlib.sha256(data.get("code_verifier", "").encode()).digest()) != code["code_challenge"]):
            self.reply(400, {"error": "invalid_grant"})
            return
        now = int(time.time())
        subject = code["fixture_subject"]
        self.reply(200, {"token_type": "Bearer", "expires_in": 300, "access_token": "synthetic-unused",
            "id_token": jwt({"iss": ISSUER, "aud": CLIENT, "sub": subject, "nonce": code["nonce"],
                "iat": now, "exp": now + 300, "email": subject + "@example.invalid", "email_verified": True,
                "name": "Synthetic " + subject})})


class Support(QuietHandler):
    def resource(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.reply(401, {"error": "unauthorized"})
            return None
        resource = self.path.removeprefix("/cases/")
        if self.path != "/cases/" + resource or resource not in {"accepted", "lost-ack", "inflight-a", "inflight-b"}:
            self.reply(404, {"error": "unknown_case"})
            return None
        return resource

    def do_GET(self):
        resource = self.resource()
        if resource:
            with LOCK:
                case = CASES.get(resource, {"id": resource, "status": "open", "version": "v1"})
                event({"kind": "read", "resource": resource, "version": case["version"]})
            self.reply(200, case)

    def do_PATCH(self):
        resource = self.resource()
        if not resource:
            return
        action = self.headers.get("Idempotency-Key", "")
        body = json.loads(self.body())
        if not action or len(action) > 128 or body != {"status": "resolved"}:
            self.reply(400, {"error": "invalid_action"})
            return
        with LOCK:
            case = CASES.get(resource, {"id": resource, "status": "open", "version": "v1"})
            event({"kind": "write_request", "resource": resource, "action_id": action})
            if action in EFFECTS:
                if EFFECTS[action]["resource"] != resource:
                    self.reply(409, {"error": "idempotency_conflict"})
                else:
                    self.reply(200, EFFECTS[action]["case"])
                return
            if self.headers.get("If-Match") != '"' + case["version"] + '"':
                self.reply(412, {"error": "version_conflict"})
                return
            case = {"id": resource, "status": "resolved", "version": "v2"}
            item = {"kind": "effect", "resource": resource, "action_id": action, "case": case}
            event(item)
            CASES[resource] = case
            EFFECTS[action] = item
        if resource == "accepted":
            self.reply(200, case)
        elif resource == "lost-ack":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        else:
            # Durable synthetic effect, withheld HTTP acknowledgment. The
            # coordinator kills the broker while this request is outstanding.
            time.sleep(15)
            self.close_connection = True


def main():
    initialize()
    identity = ThreadingHTTPServer(("127.0.0.1", 18912), Identity)
    support = ThreadingHTTPServer(("127.0.0.1", 18914), Support)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / "provider-ca.pem", ROOT / "tls-key.pem")
    support.socket = context.wrap_socket(support.socket, server_side=True)
    threading.Thread(target=identity.serve_forever, daemon=True).start()
    support.serve_forever()


if __name__ == "__main__":
    main()
