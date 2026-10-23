"""Lumiverb's AI worker (ADR-022): CLIP and face detection for another
machine's scheduler, as an AI machine in its Settings › AI.

`lumiverb-server ai-worker` runs it (src/server/lifecycle/cli.py) from any
install: the same CLIP and InsightFace the scheduler loads (its Models, the
install's models folder), no account, database or files. app.py is its API;
here, its key and its certificate: each made once, kept in the install's data
folder, owner-only.

Its key is asked of every request. Its certificate is its own (no authority
signs it): it serves HTTPS with it, and the other Lumiverb takes it and no
other, by the fingerprint it was added with (src/shared/ai_requests.py). A
worker set up before it served HTTPS has a key and no certificate: it goes
on serving plain HTTP, which the Lumiverb using it expects, until it's
started with --https (`serves_https`).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import secrets
import socket
from pathlib import Path

DEFAULT_PORT = 6366
# Its certificate is never made again on its own (the fingerprint it was added with would no longer be its own), and
# a pinned certificate's dates aren't asked about: long-lived.
CERTIFICATE_DAYS = 3650


def folder(data: Path) -> Path:
    return data / "ai-worker"


def key_file(data: Path) -> Path:
    return folder(data) / "key"


def certificate_files(data: Path) -> tuple[Path, Path]:
    """Its certificate and that certificate's private key."""
    return folder(data) / "worker.crt", folder(data) / "worker.key"


def _own_folder(data: Path) -> None:
    folder(data).mkdir(parents=True, exist_ok=True)
    os.chmod(folder(data), 0o700)


def _write(path: Path, data: bytes, mode: int) -> None:
    """Whole or not at all, `mode` from the start."""
    part = path.with_name(f".{path.name}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(part, mode)
    os.replace(part, path)


def ensure_key(data: Path) -> str:
    """The worker's key: the one kept in data, or a new one made and kept
    (owner-only) when there's none. A new worker's certificate is made with
    it, first: a worker set up now serves HTTPS from its first start."""
    path = key_file(data)
    try:
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    ensure_certificate(data)
    _own_folder(data)
    key = secrets.token_urlsafe(32)
    _write(path, (key + "\n").encode(), 0o600)
    return key


def fingerprint(data: Path) -> str | None:
    """Its certificate's SHA-256 fingerprint, as the other Lumiverb is given it (AB:CD:…); None while it has none."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from src.shared.ai_requests import fingerprint_of

    cert, key = certificate_files(data)
    try:
        if not key.exists():
            return None
        made = x509.load_pem_x509_certificate(cert.read_bytes())
    except (OSError, ValueError):
        return None
    return fingerprint_of(made.public_bytes(serialization.Encoding.DER))


def ensure_certificate(data: Path) -> str:
    """Its certificate's fingerprint: the one kept in data, or a new certificate made and kept (its key owner-only)
    when there's none. Made once: its own, signed by nobody, for this machine's names and addresses then."""
    if (made := fingerprint(data)) is not None:
        return made
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from src.server.lifecycle.tls import names_here

    _own_folder(data)
    now = dt.datetime.now(dt.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         f"Lumiverb AI worker on {socket.gethostname().split('.')[0]}"[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lumiverb")])
    alt: list[x509.GeneralName] = []
    for each in names_here():
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(each)))
        except ValueError:
            try:
                alt.append(x509.DNSName(each))
            except ValueError:  # not a name a certificate can carry (not ASCII)
                continue
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=CERTIFICATE_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = certificate_files(data)
    _write(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()), 0o600)
    _write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return fingerprint(data) or ""


def serves_https(data: Path, *, https: bool = False, plain_http: bool = False) -> bool:
    """Whether it serves HTTPS: unless asked for plain HTTP (--plain-http); when asked for HTTPS (--https, its
    certificate made then if it has none); else when it has its certificate. One set up before the worker served
    HTTPS has a key and no certificate: plain HTTP, as the Lumiverb using it was given its http:// address, until
    it's started with --https. Call after ensure_key (a new worker's certificate is made with its key)."""
    if plain_http:
        return False
    return https or fingerprint(data) is not None
