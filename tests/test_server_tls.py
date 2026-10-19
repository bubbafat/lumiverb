"""The certificate other devices are served (src/server/lifecycle/tls.py):
Lumiverb's own authority's for this machine's names, issued again when they
change or it nears its end; or one an admin gives, checked first. A device
that trusts the authority verifies it (a real handshake, Python's ssl), for
those names and no others. Through real nginx: test_server_run.py."""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import stat
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from src.server.lifecycle import tls

pytestmark = pytest.mark.fast

NAMES = ("macstudio.local", "macstudio", "192.168.86.55", "localhost", "127.0.0.1", "::1")
# Now: a real handshake checks the certificate against this machine's clock.
NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)


def _handshake(served: tls.Served, ca: Path, hostname: str) -> None:
    """A client trusting `ca` connects to a server serving `served`, asking for `hostname`."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(served.certificate, served.key)
    client_context = ssl.create_default_context(cafile=str(ca))
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    errors: list[BaseException] = []

    def serve():
        try:
            conn, _ = listener.accept()
            with server_context.wrap_socket(conn, server_side=True) as tls_conn:
                tls_conn.recv(1)
        except (ssl.SSLError, OSError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=30) as raw, \
                client_context.wrap_socket(raw, server_hostname=hostname) as conn:
            conn.sendall(b"x")
    finally:
        listener.close()
        thread.join(60)


def test_lumiverb_s_own_certificate_is_trusted_by_a_device_that_trusts_its_authority(tmp_path):
    served = tls.served(tmp_path, NAMES, NOW)
    assert served.source == tls.LUMIVERB and set(served.names) == set(NAMES)
    for name in ("macstudio.local", "192.168.86.55", "localhost"):
        _handshake(served, tls.ca_certificate(tmp_path), name)
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(served, tls.ca_certificate(tmp_path), "elsewhere.local")


def test_a_device_that_doesnt_trust_the_authority_refuses_it(tmp_path):
    served = tls.served(tmp_path, NAMES, NOW)
    other = tls.served(tmp_path / "other", NAMES, NOW)
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(served, tls.ca_certificate(tmp_path / "other"), "macstudio.local")
    assert other.fingerprint != served.fingerprint


def test_its_certificate_is_a_server_s_and_lasts_397_days(tmp_path):
    served = tls.served(tmp_path, NAMES, NOW)
    cert = x509.load_pem_x509_certificate(served.certificate.read_bytes())
    assert cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage(
        [ExtendedKeyUsageOID.SERVER_AUTH])
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert cert.not_valid_after_utc - NOW == dt.timedelta(days=tls.SERVER_DAYS)
    ca = x509.load_pem_x509_certificate(tls.ca_certificate(tmp_path).read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True


def test_the_keys_are_this_machine_s_user_s_alone(tmp_path):
    tls.served(tmp_path, NAMES, NOW)
    for key in ("ca.key", "server.key"):
        assert stat.S_IMODE((tmp_path / "tls" / key).stat().st_mode) == 0o600, key


def test_it_s_issued_again_only_when_it_has_to_be(tmp_path):
    first = tls.served(tmp_path, NAMES, NOW)
    assert tls.served(tmp_path, NAMES, NOW + dt.timedelta(days=100)).fingerprint == first.fingerprint
    moved = tls.served(tmp_path, (*NAMES[:2], "192.168.86.77", *NAMES[3:]), NOW + dt.timedelta(days=100))
    assert moved.fingerprint != first.fingerprint and "192.168.86.77" in moved.names, "a new address from the router"
    renewed = tls.served(tmp_path, moved.names, moved.not_after - dt.timedelta(days=10))
    assert renewed.fingerprint != moved.fingerprint, "a month before its end"
    ca = tls.ca_certificate(tmp_path).read_bytes()
    tls.served(tmp_path, NAMES, NOW + dt.timedelta(days=200))
    assert tls.ca_certificate(tmp_path).read_bytes() == ca, "the authority is made once: devices trust it once"


def test_a_certificate_the_authority_didnt_sign_is_issued_again(tmp_path):
    first = tls.served(tmp_path, NAMES, NOW)
    (tmp_path / "tls" / "ca.crt").unlink()
    (tmp_path / "tls" / "ca.key").unlink()
    again = tls.served(tmp_path, NAMES, NOW)
    assert again.fingerprint != first.fingerprint
    _handshake(again, tls.ca_certificate(tmp_path), "macstudio.local")


# -- Yours -------------------------------------------------------------------------------------


def _theirs(names=("photos.example.com",), *, days: int = 90, start: dt.datetime = NOW) -> tuple[str, str, str]:
    """A certificate from another authority (as a public one's would be): its PEM, its key's, the authority's."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Elsewhere CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(start - dt.timedelta(days=1)).not_valid_after(start + dt.timedelta(days=900))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "x")]))
               .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
               .not_valid_before(start).not_valid_after(start + dt.timedelta(days=days)))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())
    pem = (cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return pem, key_pem, ca.public_bytes(serialization.Encoding.PEM).decode()


def test_yours_is_served_instead_and_lumiverb_s_own_again_once_it_s_taken_away(tmp_path):
    ours = tls.served(tmp_path, NAMES, NOW)
    pem, key, their_ca = _theirs()
    kept = tls.keep_yours(tmp_path, pem, key, NOW)
    assert (kept.source, kept.names) == (tls.YOURS, ("photos.example.com",))
    assert tls.served(tmp_path, NAMES, NOW).source == tls.YOURS
    (tmp_path / "theirs.pem").write_text(their_ca)
    _handshake(tls.served(tmp_path, NAMES, NOW), tmp_path / "theirs.pem", "photos.example.com")
    assert stat.S_IMODE((tmp_path / "tls" / "yours.key").stat().st_mode) == 0o600
    tls.forget_yours(tmp_path)
    assert tls.served(tmp_path, NAMES, NOW).fingerprint == ours.fingerprint


def test_yours_once_ended_isn_t_served(tmp_path):
    pem, key, _ = _theirs(days=30)
    tls.keep_yours(tmp_path, pem, key, NOW)
    later = NOW + dt.timedelta(days=31)
    assert tls.served(tmp_path, NAMES, later).source == tls.LUMIVERB
    assert tls.describe(tmp_path, later).source == tls.LUMIVERB, "Settings says what's really served"


@pytest.mark.parametrize(("change", "says"), [
    (lambda pem, key: ("not a certificate", key), "isn't a certificate"),
    (lambda pem, key: (pem, "not a key"), "isn't a private key"),
    (lambda pem, key: (pem, _theirs()[1]), "isn't the certificate's"),
])
def test_yours_is_refused_saying_why(change, says):
    pem, key, _ = _theirs()
    with pytest.raises(tls.TlsError, match=says):
        tls.check_yours(*change(pem, key), NOW)


def test_yours_ended_not_started_or_naming_no_host_is_refused():
    with pytest.raises(tls.TlsError, match="ended"):
        tls.check_yours(*_theirs(days=10)[:2], NOW + dt.timedelta(days=11))
    with pytest.raises(tls.TlsError, match="only starts"):
        tls.check_yours(*_theirs(start=NOW + dt.timedelta(days=5))[:2], NOW)
    with pytest.raises(tls.TlsError, match="names no host"):
        tls.check_yours(*_theirs(names=())[:2], NOW)


def test_a_key_with_a_passphrase_is_refused():
    pem, key, _ = _theirs()
    private = serialization.load_pem_private_key(key.encode(), password=None)
    locked = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.BestAvailableEncryption(b"secret")).decode()
    with pytest.raises(tls.TlsError, match="passphrase"):
        tls.check_yours(pem, locked, NOW)


def test_nothing_is_described_before_run_made_one(tmp_path):
    assert tls.describe(tmp_path) is None


def test_this_machine_s_names(monkeypatch):
    from src.server.lifecycle import network

    monkeypatch.setattr(tls.socket, "gethostname", lambda: "MacStudio.local")
    monkeypatch.setattr(network, "lan_address", lambda: "192.168.86.55")
    assert tls.names_here() == ("MacStudio.local", "MacStudio", "192.168.86.55", "localhost", "127.0.0.1", "::1")
    monkeypatch.setattr(network, "lan_address", lambda: None)
    assert "192.168.86.55" not in tls.names_here(), "no network: its names only"
