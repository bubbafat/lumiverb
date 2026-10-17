"""The certificate a Mac's Lumiverb serves other devices on its network with
(Robert, 2026-10-15: "Own CA + your certificate"; Tailscale never needed).

Other devices reach it over plain HTTP, and over HTTPS too once an admin
adds it (network.py: its own port, WEB_TLS_PORT else 6443; nothing is made
here until then); this machine's own browser keeps plain HTTP on localhost.
The certificate is one of two, in DATA_DIR/tls:

- **Lumiverb's own** (the default): a certificate authority of this install,
  made once (`ca.crt`, its key `ca.key`), issues the server's (`server.crt`)
  for the names other devices use: this Mac's `.local` name, its name, its
  address on the network, and localhost. It's issued again when those
  change (a new address from the router) and a month before it ends. Each
  device trusts the authority once: Settings › Network offers `ca.crt`
  (served at /lumiverb-ca.crt, signed in or not: it's public).
- **Yours**: a certificate and key an admin gives (from any authority the
  devices trust: public hosting), checked before it's kept (`yours.crt`,
  `yours.key`), served instead until it's taken away.

Keys are this machine's user's alone (0600). run issues and renews
(`served`, at start and on its watch); the API keeps or forgets yours and
says what's served (`describe`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TLS_DIR = "tls"
CA_DAYS = 3650
# Apple's limit for a server certificate from an authority a person added
# (825 days); kept to a public authority's, 397, so it's never the reason.
SERVER_DAYS = 397
RENEW_BEFORE = dt.timedelta(days=30)
LUMIVERB, YOURS = "lumiverb", "yours"


class TlsError(ValueError):
    """A certificate or key that can't be served, saying why."""


@dataclass(frozen=True)
class Served:
    certificate: Path
    key: Path
    source: str  # LUMIVERB or YOURS
    names: tuple[str, ...]
    not_after: dt.datetime
    fingerprint: str  # SHA-256 of the certificate (DER), hex


def folder(data_dir: Path) -> Path:
    return data_dir / TLS_DIR


def ca_certificate(data_dir: Path) -> Path:
    return folder(data_dir) / "ca.crt"


def ca_fingerprint(data_dir: Path) -> str | None:
    """The authority's SHA-256 fingerprint, as a device shows it (pairs of hex,
    colon-separated), for a person to compare: the certificate reaches a
    device over plain HTTP. None before run made it."""
    try:
        cert = x509.load_pem_x509_certificate(ca_certificate(data_dir).read_bytes())
    except (OSError, ValueError):
        return None
    digest = _fingerprint(cert).upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def names_here() -> tuple[str, ...]:
    """What other devices call this machine: its Bonjour name (`.local`), its
    name, its address on the network; and localhost, as this machine does."""
    host = socket.gethostname().rstrip(".")
    short = host.split(".")[0]
    names = [f"{short}.local", short] + ([host] if host not in (short, f"{short}.local") else [])
    from src.server.lifecycle.network import lan_address

    if (address := lan_address()) is not None:
        names.append(address)
    return tuple(dict.fromkeys([*names, "localhost", "127.0.0.1", "::1"]))


# -- Files ------------------------------------------------------------------------------------


def _write(path: Path, data: bytes, *, private: bool = False) -> None:
    """Whole or not at all; a key its owner's alone from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(part, path)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _names_of(cert: x509.Certificate) -> tuple[str, ...]:
    try:
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return ()
    return tuple([*alt.get_values_for_type(x509.DNSName), *(str(ip) for ip in alt.get_values_for_type(x509.IPAddress))])


def _first_cert(pem: bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificates(pem)[0]


def _served(cert_path: Path, key_path: Path, source: str) -> Served:
    cert = _first_cert(cert_path.read_bytes())
    return Served(cert_path, key_path, source, _names_of(cert), cert.not_valid_after_utc, _fingerprint(cert))


# -- Lumiverb's own authority -----------------------------------------------------------------


def _authority(data_dir: Path, now: dt.datetime) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """This install's authority: made once, kept."""
    cert_path, key_path = ca_certificate(data_dir), folder(data_dir) / "ca.key"
    if cert_path.exists() and key_path.exists():
        return (x509.load_pem_x509_certificate(cert_path.read_bytes()),
                serialization.load_pem_private_key(key_path.read_bytes(), password=None))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Lumiverb on {socket.gethostname().split('.')[0]}"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lumiverb")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write(key_path, _key_pem(key), private=True)
    _write(cert_path, _cert_pem(cert))
    return cert, key


def _alt_names(names: tuple[str, ...]) -> list[x509.GeneralName]:
    out: list[x509.GeneralName] = []
    for name in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            out.append(x509.DNSName(name))
    return out


def _issue(data_dir: Path, names: tuple[str, ...], now: dt.datetime) -> Served:
    ca, ca_key = _authority(data_dir, now)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
            .add_extension(x509.SubjectAlternativeName(_alt_names(names)), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    key_path, cert_path = folder(data_dir) / "server.key", folder(data_dir) / "server.crt"
    _write(key_path, _key_pem(key), private=True)
    _write(cert_path, _cert_pem(cert))
    return _served(cert_path, key_path, LUMIVERB)


# -- Yours ------------------------------------------------------------------------------------


def _yours_paths(data_dir: Path) -> tuple[Path, Path]:
    return folder(data_dir) / "yours.crt", folder(data_dir) / "yours.key"


def check_yours(certificate: str, key: str, now: dt.datetime | None = None) -> x509.Certificate:
    """The first certificate of `certificate` (PEM, with its chain), when it
    and `key` (PEM, no passphrase) can be served now. TlsError says why not."""
    now = now or dt.datetime.now(dt.UTC)
    try:
        certs = x509.load_pem_x509_certificates(certificate.encode())
    except ValueError:
        raise TlsError("That isn't a certificate in PEM form (it starts with -----BEGIN CERTIFICATE-----).") from None
    try:
        private = serialization.load_pem_private_key(key.encode(), password=None)
    except TypeError:
        raise TlsError("The key has a passphrase: give it without one.") from None
    except ValueError:
        raise TlsError("That isn't a private key in PEM form (it starts with -----BEGIN … PRIVATE KEY-----).") from None
    if not isinstance(private, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise TlsError("The key must be an RSA or ECDSA key.")
    cert = certs[0]
    der = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if cert.public_key().public_bytes(*der) != private.public_key().public_bytes(*der):
        raise TlsError("The key isn't the certificate's: give the key it was made with.")
    if cert.not_valid_after_utc <= now:
        raise TlsError(f"The certificate ended on {cert.not_valid_after_utc:%b %-d, %Y}.")
    if cert.not_valid_before_utc > now:
        raise TlsError(f"The certificate only starts on {cert.not_valid_before_utc:%b %-d, %Y}.")
    if not _names_of(cert):
        raise TlsError("The certificate names no host (it has no subject alternative names): browsers won't take it.")
    return cert


def keep_yours(data_dir: Path, certificate: str, key: str, now: dt.datetime | None = None) -> Served:
    """Yours checked (check_yours), then kept: served from run's next look."""
    check_yours(certificate, key, now)
    cert_path, key_path = _yours_paths(data_dir)
    _write(key_path, key.encode(), private=True)
    _write(cert_path, certificate.encode())
    return _served(cert_path, key_path, YOURS)


def forget_yours(data_dir: Path) -> None:
    """Back to Lumiverb's own."""
    for path in _yours_paths(data_dir):
        path.unlink(missing_ok=True)


# -- What's served ------------------------------------------------------------------------------


def describe(data_dir: Path, now: dt.datetime | None = None) -> Served | None:
    """What's served now, as served() would choose it but issuing nothing:
    yours while it can be, else Lumiverb's own; None before run made one."""
    yours_cert, yours_key = _yours_paths(data_dir)
    try:
        if yours_cert.exists() and yours_key.exists():
            check_yours(yours_cert.read_text(), yours_key.read_text(), now)
            return _served(yours_cert, yours_key, YOURS)
    except (OSError, TlsError):
        pass
    server_cert, server_key = folder(data_dir) / "server.crt", folder(data_dir) / "server.key"
    try:
        if server_cert.exists() and server_key.exists():
            return _served(server_cert, server_key, LUMIVERB)
    except (OSError, ValueError):
        return None
    return None


def served(data_dir: Path, names: tuple[str, ...] | None = None, now: dt.datetime | None = None) -> Served:
    """What to serve: yours when there is one that still can be; else
    Lumiverb's own, issued again when its names aren't these (`names`, this
    machine's now), it ends within RENEW_BEFORE, or this install's authority
    didn't sign it. run's: at start, then on its watch."""
    now = now or dt.datetime.now(dt.UTC)
    yours_cert, yours_key = _yours_paths(data_dir)
    if yours_cert.exists() and yours_key.exists():
        try:
            check_yours(yours_cert.read_text(), yours_key.read_text(), now)
            return _served(yours_cert, yours_key, YOURS)
        except (OSError, TlsError):
            pass  # ended, or broken since: Lumiverb's own meanwhile (Settings says which is served)
    names = names or names_here()
    cert_path, key_path = folder(data_dir) / "server.crt", folder(data_dir) / "server.key"
    try:
        current = _served(cert_path, key_path, LUMIVERB) if cert_path.exists() and key_path.exists() else None
    except (OSError, ValueError):
        current = None
    if (current is not None and set(current.names) == set(names) and current.not_after - now > RENEW_BEFORE
            and _issued_by_ours(data_dir, cert_path)):
        return current
    return _issue(data_dir, names, now)


def _issued_by_ours(data_dir: Path, cert_path: Path) -> bool:
    """Signed by this install's authority (made again, say: then so is the certificate)."""
    try:
        ca = x509.load_pem_x509_certificate(ca_certificate(data_dir).read_bytes())
        _first_cert(cert_path.read_bytes()).verify_directly_issued_by(ca)
        return True
    except Exception:  # noqa: BLE001 — unreadable, or not ours
        return False
