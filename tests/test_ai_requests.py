"""Every request to an AI machine goes to the address the machine is at, and
nowhere else (src/shared/ai_requests.py): no redirect is followed, by any
kind of request (descriptions, text, a video's description, transcripts,
Lumiverb's AI worker, a models list, the CLI's); the machine's name is
checked again at each request, and the address each connection reached
before anything is sent on it, by the rule a machine is added by
(vision_endpoint.allowed). An https machine added with its certificate's
fingerprint is reached with that certificate and no other, whoever signed
it: a connection with another is closed after its handshake, before
anything is sent on it, and it's the machine's fault, said plainly. What a
machine says when it fails is the log's: the error, which every signed-in
person sees as the machine's status, why work waits or a clip's failure,
says what went wrong in Lumiverb's words.

The machines are real HTTP and HTTPS servers on this computer
(tests/ai_machine_server.py), with certificates made as Lumiverb's AI worker
makes its own.
"""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from PIL import Image

from src.processing.ai_worker_client import AiWorker
from src.processing.workers.captions.base import CaptionError
from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider
from src.processing.workers.embeddings.base import EmbeddingError
from src.processing.workers.faces import FaceError
from src.processing.workers.transcripts.base import TranscriptError
from src.processing.workers.transcripts.openai_compatible import OpenAITranscriber
from src.shared import ai_requests
from src.shared.vision_endpoint import (
    NOT_ITS_CERTIFICATE,
    UNREACHABLE,
    UNTRUSTED,
    VisionEndpointError,
    allowed,
    list_models,
)
from tests.ai_machine_server import MODEL, WORKS, certificate, local_machine, redirecting

pytestmark = pytest.mark.fast

REDIRECT = "answered with a redirect, which Lumiverb doesn't follow."
NOT_ALLOWED = "is at an address that isn't allowed."


@pytest.fixture
def no_waiting():
    """The caption provider's retries, without their sleeps."""
    with patch.object(OpenAICompatibleCaptionProvider, "_sleep_with_countdown"):
        yield


def _picture(tmp_path: Path) -> Path:
    path = tmp_path / "picture.jpg"
    Image.new("RGB", (16, 16), (200, 30, 30)).save(path)
    return path


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _wav(tmp_path: Path) -> Path:
    import wave

    path = tmp_path / "speech.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * 16_000)
    return path


# What each kind of request raises, and whether it was the machine's fault. fingerprint: the certificate an https
# machine was added with.
def _describe(url, tmp_path, fingerprint=""):
    return OpenAICompatibleCaptionProvider(url, MODEL, "sk-1", fingerprint=fingerprint).describe(_picture(tmp_path))


def _describe_text(url, tmp_path, fingerprint=""):
    return OpenAICompatibleCaptionProvider(url, MODEL, "sk-1", fingerprint=fingerprint).describe_text(
        "1. (0:00) Waves.")


def _read_text(url, tmp_path, fingerprint=""):
    return OpenAICompatibleCaptionProvider(url, MODEL, "sk-1", fingerprint=fingerprint).extract_text(
        _picture(tmp_path))


def _transcribe(url, tmp_path, fingerprint=""):
    return OpenAITranscriber(url, "sk-1", "small", fingerprint=fingerprint).transcribe(_wav(tmp_path))


def _clip(url, tmp_path, fingerprint=""):
    return AiWorker(url, "sk-1", fingerprint=fingerprint).clip(_png(), "ViT-B-32")


def _faces(url, tmp_path, fingerprint=""):
    return AiWorker(url, "sk-1", fingerprint=fingerprint, error=FaceError).faces(_png(), {"level": "normal"})


WORK = {"describe": (_describe, CaptionError), "describe_text": (_describe_text, CaptionError),
        "read_text": (_read_text, CaptionError), "transcribe": (_transcribe, TranscriptError),
        "vector": (_clip, EmbeddingError), "found_faces": (_faces, FaceError)}


@pytest.mark.parametrize("status", [302, 307])
@pytest.mark.parametrize("kind", list(WORK))
def test_a_machine_that_answers_its_work_with_a_redirect_is_at_fault_and_it_isnt_followed(
        tmp_path, no_waiting, kind, status):
    work, error = WORK[kind]
    with redirecting(status) as (machine, elsewhere), pytest.raises(error) as raised:
        work(machine.url, tmp_path)
    assert machine.asked, "the machine was asked"
    assert elsewhere.asked == [], "nothing went where it pointed"
    assert raised.value.endpoint_fault is True
    assert str(raised.value).endswith(REDIRECT) and str(raised.value).startswith(machine.url), str(raised.value)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_models_list_answered_with_a_redirect_isnt_followed(status, caplog):
    """Connect, adding a machine and the scheduler's check say it as they say every failure (so they can't map
    a network); what happened is the log's, and the server admin's (the error's detail)."""
    with redirecting(status, lists=None) as (machine, elsewhere), \
            caplog.at_level(logging.WARNING), pytest.raises(VisionEndpointError) as raised:
        list_models(machine.url, "sk-1")
    assert elsewhere.asked == [] and machine.asked == [("GET", "/v1/models")]
    assert str(raised.value) == UNREACHABLE
    assert raised.value.detail == f"{machine.url}: /models answered with a redirect, which Lumiverb doesn't follow"
    assert elsewhere.url + "/somewhere" in caplog.text, "where it pointed is the log's"


def test_the_clis_model_discovery_doesnt_follow_one_either():
    from src.processing.workers.captions.model_discovery import clear_cache, discover_model_id

    clear_cache()
    with redirecting(302, lists=None) as (machine, elsewhere), pytest.raises(ai_requests.Redirected):
        discover_model_id(machine.url)
    assert elsewhere.asked == []


def test_a_machine_that_redirects_its_work_is_marked_at_fault_and_says_so_plainly(tmp_path, no_waiting):
    """The scheduler's pool: the machine lists the model, so it's online, then answers its work with a redirect.
    Its work stops there (none goes elsewhere), and the status every signed-in person sees says why in a
    sentence."""
    from src.processing.vision_guard import VisionGuard

    with redirecting(307) as (machine, elsewhere):
        client = MagicMock()
        client.get.return_value.json.return_value = {"job": "vision", "model": MODEL, "machines": [
            {"machine_id": "aim_studio", "name": "Studio", "api_url": machine.url, "api_key": "sk-1", "at_once": 1}]}
        guard = VisionGuard(client)
        assert guard.check() is True, guard.error
        with pytest.raises(CaptionError) as raised:
            guard.provider().describe(_picture(tmp_path))
    assert elsewhere.asked == []
    assert raised.value.endpoint_fault and guard.charges(raised.value) is False, "no clip is charged"
    reported = [c.kwargs["json"] for c in client.post.call_args_list if c.args[0] == "/v1/ai/machines/aim_studio/status"]
    assert reported[0] == {"online": True, "error": "", "models": [MODEL]}
    assert reported[-1] == {"online": False, "error": f"{machine.url}/chat/completions {REDIRECT}", "models": []}


class _Listener:
    """A TCP port on this computer that keeps what each connection sent it."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                got = b""
                try:
                    while chunk := conn.recv(65536):
                        got += chunk
                except OSError:
                    pass
                self.received.append(got)

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def listener():
    made = _Listener()
    yield made
    made.close()


def _moves(host: str, first: str, then: str):
    """getaddrinfo for host: `first` at the first lookup, `then` after (a name that resolves elsewhere by the time
    it's connected to). Every other name as it is."""
    real = socket.getaddrinfo
    seen: list[str] = []

    def getaddrinfo(name, port, *args, **kwargs):
        if name != host:
            return real(name, port, *args, **kwargs)
        ip = first if not seen else then
        seen.append(ip)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    return patch("socket.getaddrinfo", side_effect=getaddrinfo), seen


# Loopback stands in for a link-local or cloud metadata address here: nothing on this computer listens on one.
REFUSES_LOOPBACK = patch.object(ai_requests, "BLOCKED", (ipaddress.ip_network("127.0.0.0/8"),))


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_nothing_is_sent_on_a_connection_to_an_address_the_rule_refuses(listener, scheme):
    """The name looked fine when it was checked, then resolved to an address no AI machine is at: the connection
    is closed before anything is sent on it (an https one before its TLS handshake), and it's the machine's fault."""
    resolves, seen = _moves("ollama.test", "10.0.0.5", "127.0.0.1")
    url = f"{scheme}://ollama.test:{listener.port}/v1/chat/completions"
    with resolves, REFUSES_LOOPBACK:
        with pytest.raises(ai_requests.AddressRefused) as raised:
            ai_requests.post(url, json={"model": MODEL}, headers={"Authorization": "Bearer sk-1"}, timeout=5)
        with pytest.raises(VisionEndpointError, match="That address isn't allowed."):
            allowed(f"http://127.0.0.1:{listener.port}/v1")  # the rule a machine is added by
    assert seen[:2] == ["10.0.0.5", "127.0.0.1"], "checked by name first, then connected"
    assert str(raised.value) == f"{url} {NOT_ALLOWED}"
    assert isinstance(raised.value, requests.ConnectionError), "unreachable, as far as the work goes"
    deadline = __import__("time").monotonic() + 5
    while not listener.received and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert listener.received and all(got == b"" for got in listener.received), "the key and the work went nowhere"


def test_work_on_a_connection_the_rule_refuses_is_the_machines_fault(listener, tmp_path, no_waiting):
    url = f"http://ollama.test:{listener.port}/v1"
    for work, error in WORK.values():
        resolves, _ = _moves("ollama.test", "10.0.0.5", "127.0.0.1")
        with resolves, REFUSES_LOOPBACK, pytest.raises(error) as raised:
            work(url, tmp_path)
        assert raised.value.endpoint_fault is True and str(raised.value).endswith(NOT_ALLOWED), str(raised.value)
    assert all(got == b"" for got in listener.received)


def test_a_name_that_resolves_to_a_refused_address_is_never_connected_to():
    """Checked again at each request, not only when the machine was added."""
    resolves, _ = _moves("ollama.test", "169.254.169.254", "169.254.169.254")
    with resolves, patch("requests.Session.request", side_effect=AssertionError("connected")), \
            pytest.raises(ai_requests.AddressRefused):
        ai_requests.get("http://ollama.test:11434/v1/models", timeout=5)


def test_loopback_and_the_lan_are_machines_as_they_always_were():
    """Ollama on this computer or the LAN: the rule refuses only link-local and cloud metadata addresses."""
    for address in ("127.0.0.1", "::1", "10.0.0.5", "192.168.1.20", "172.18.0.6"):
        assert ai_requests.refused(address) is False, address
    for address in ("169.254.169.254", "fe80::1", "fe80::1%eth0", "fd00:ec2::254", "::ffff:169.254.169.254"):
        assert ai_requests.refused(address) is True, address
    with local_machine() as machine:
        assert ai_requests.post(machine.url + "/chat/completions", json={}, timeout=5).json() == WORKS


SECRET = "upstream db 10.1.2.3:5432 refused user admin"


@pytest.mark.parametrize(("status", "says"), [
    (500, "answered 500."), (503, "answered 503."), (401, "refused the key."),
    (404, f"has no {MODEL} or no chat completions API there."), (400, "answered 400."),
])
def test_what_a_machine_says_with_a_failure_is_the_logs_not_the_errors(tmp_path, no_waiting, caplog, status, says):
    with local_machine((status, {"error": {"message": SECRET}}, {})) as machine, \
            caplog.at_level(logging.WARNING), pytest.raises(CaptionError) as raised:
        _describe(machine.url, tmp_path)
    assert str(raised.value) == f"{machine.url}/chat/completions {says}"
    assert SECRET in caplog.text


def test_a_reply_that_isnt_a_description_stays_out_of_the_clips_failure(tmp_path, no_waiting, caplog):
    reply = {"choices": [{"message": {"content": SECRET}, "finish_reason": "stop"}]}
    with local_machine((200, reply, {})) as machine, caplog.at_level(logging.WARNING), \
            pytest.raises(CaptionError) as raised:
        _describe(machine.url, tmp_path)
    assert raised.value.endpoint_fault is False and SECRET not in str(raised.value)
    assert "No JSON object found" in str(raised.value) and SECRET in caplog.text


@pytest.mark.parametrize(("answer", "error", "fault", "says"), [
    ((500, {"error": {"code": "internal", "message": SECRET}}, {}), EmbeddingError, True, "answered 500"),
    ((401, {"error": {"code": "unauthorized", "message": SECRET}}, {}), EmbeddingError, True, "refused the key"),
    ((413, {"error": {"code": "too_large", "message": SECRET}}, {}), EmbeddingError, False, "answered 413"),
    ((200, {"results": [{"error": SECRET}]}, {}), EmbeddingError, False, "couldn't read the image"),
    ((200, {"results": [{"error": SECRET}]}, {}), FaceError, False, "couldn't read the image"),
])
def test_what_an_ai_worker_says_with_a_failure_is_the_logs(caplog, answer, error, fault, says):
    work = _clip if error is EmbeddingError else _faces
    with local_machine(answer) as machine, caplog.at_level(logging.WARNING), pytest.raises(error) as raised:
        work(machine.url, None)
    assert raised.value.endpoint_fault is fault
    assert str(raised.value) == f"{machine.url} {says}" and SECRET in caplog.text


NOT_ITS = "has a certificate that isn't the one it was added with."


@pytest.fixture
def certificates(tmp_path):
    """The machine's certificate (as Lumiverb's AI worker makes its own: signed by nobody), and another's."""
    return certificate(tmp_path / "machine"), certificate(tmp_path / "another")


@pytest.mark.parametrize("kind", list(WORK))
def test_every_kind_of_work_reaches_a_machine_over_https_by_the_certificate_it_was_added_with(
        tmp_path, no_waiting, certificates, kind):
    """Its own certificate, which no authority signed and which names nobody here: taken, as it's the one it was
    added with. Another one, as good, isn't: nothing is asked of it, and it's the machine's fault, said plainly."""
    work, error = WORK[kind]
    (files, pinned), (_, another) = certificates
    with local_machine(tls=files) as machine:
        assert machine.url.startswith("https://")
        work(machine.url, tmp_path, pinned)
        assert machine.asked and all(method == "POST" for method, _ in machine.asked)
        machine.asked.clear()
        with pytest.raises(error) as raised:
            work(machine.url, tmp_path, another)
    assert machine.asked == [], "nothing reached it"
    assert raised.value.endpoint_fault is True and str(raised.value).endswith(NOT_ITS), str(raised.value)
    assert str(raised.value).startswith(machine.url)


def test_a_models_list_is_asked_by_the_certificate_and_one_without_it_is_checked_as_any_https_sites(
        certificates, caplog):
    """Connect, adding a machine and the scheduler's check: its fingerprint, and it lists its models; another's,
    said plainly; none, and its certificate is checked as any https site's is, which one signed by nobody isn't."""
    (files, pinned), (_, another) = certificates
    with local_machine(tls=files) as machine, caplog.at_level(logging.WARNING):
        assert list_models(machine.url, "sk-1", fingerprint=pinned) == [MODEL]
        with pytest.raises(VisionEndpointError) as raised:
            list_models(machine.url, "sk-1", fingerprint=another)
        assert str(raised.value) == NOT_ITS_CERTIFICATE == "Its certificate isn't the one it was added with."
        with pytest.raises(VisionEndpointError) as raised:
            list_models(machine.url, "sk-1")
        assert str(raised.value) == UNTRUSTED
    assert machine.asked == [("GET", "/v1/models")]
    assert pinned in caplog.text or another in caplog.text, "which certificate it had is the log's"


class _TLSListener:
    """A TLS port on this computer with the machine's certificate, that keeps what each connection sent it once its
    handshake was done, and answers a request it's sent whole."""

    def __init__(self, files) -> None:
        import ssl

        self.handshakes = 0
        self.received: list[bytes] = []
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(*map(str, files))
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        import ssl

        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    tls = self._context.wrap_socket(conn, server_side=True)
                except (OSError, ssl.SSLError):
                    continue
                self.handshakes += 1
                got = b""
                try:
                    while chunk := tls.recv(65536):
                        got += chunk
                        head, ended, body = got.partition(b"\r\n\r\n")
                        length = int(next((line.split(b":", 1)[1] for line in head.split(b"\r\n")
                                           if line.lower().startswith(b"content-length:")), b"0"))
                        if ended and len(body) >= length:
                            answer = json.dumps(WORKS).encode()
                            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                                        + str(len(answer)).encode() + b"\r\nConnection: close\r\n\r\n" + answer)
                            break
                except (OSError, ssl.SSLError):
                    pass
                self.received.append(got)

    def close(self) -> None:
        self._sock.close()


def _settled(listener, connections: int) -> None:
    deadline = __import__("time").monotonic() + 5
    while len(listener.received) < connections and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)


def test_a_certificate_that_isnt_the_one_it_was_added_with_gets_nothing_sent_to_it(certificates, caplog):
    """The connection reaches its handshake (where its certificate is seen) and is closed there: neither the request,
    its key nor the work goes on it. With the right one, the same request arrives whole."""
    (files, pinned), (_, another) = certificates
    listener = _TLSListener(files)
    url = f"https://127.0.0.1:{listener.port}/v1/lumiverb/clip"
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(ai_requests.NotItsCertificate) as raised:
            ai_requests.post(url, headers={"Authorization": "Bearer sk-1"}, data={"model": "ViT-B-32"},
                             files=[("images", ("0.img", _png(), "application/octet-stream"))], timeout=5,
                             fingerprint=another)
        _settled(listener, 1)
        assert listener.handshakes == 1, "it got as far as the certificate"
        assert listener.received == [b""], "and not a byte after it"
        assert str(raised.value) == f"{url} {NOT_ITS}"
        assert isinstance(raised.value, requests.ConnectionError), "unreachable, as far as the work goes"
        assert pinned in caplog.text and another in caplog.text, "which it had, and which it was added with: the log's"

        r = ai_requests.post(url, headers={"Authorization": "Bearer sk-1"}, data={"model": "ViT-B-32"},
                             files=[("images", ("0.img", _png(), "application/octet-stream"))], timeout=5,
                             fingerprint=pinned)
        assert r.status_code == 200 and r.json() == WORKS
        _settled(listener, 2)
        assert listener.handshakes == 2
        sent = listener.received[1]
        assert sent.startswith(b"POST /v1/lumiverb/clip ") and b"Bearer sk-1" in sent and _png() in sent
    finally:
        listener.close()


def test_a_fingerprint_is_taken_typed_any_way_and_kept_one_way():
    kept = "AB:" * 31 + "CD"
    for typed in (kept, kept.lower(), kept.replace(":", ""), kept.replace(":", " "), f"  {kept.lower()}\n"):
        assert ai_requests.fingerprint_typed(typed) == kept and ai_requests.FINGERPRINT.fullmatch(kept)
    for wrong in ("", "AB:CD", kept + ":EF", kept[:-1] + "G", "sha256:" + kept.replace(":", "")):
        with pytest.raises(ValueError, match="SHA-256"):
            ai_requests.fingerprint_typed(wrong)
    assert ai_requests.fingerprint_of(b"certificate") == ai_requests.fingerprint_typed(
        __import__("hashlib").sha256(b"certificate").hexdigest())


def test_a_fingerprint_is_an_https_machines_alone(listener):
    """Never sent to an http address, where it would check nothing."""
    with pytest.raises(ValueError, match="https"):
        ai_requests.get(f"http://127.0.0.1:{listener.port}/v1/models", timeout=5, fingerprint="AB:" * 31 + "CD")
    assert listener.received == []


def test_a_pinned_certificate_never_goes_through_a_socks_proxy(monkeypatch, certificates):
    """A SOCKS proxy's connections aren't Lumiverb's to check: a request whose certificate must be compared doesn't
    take one (an http proxy's tunnel is: the certificate is compared through it)."""
    (files, pinned), _ = certificates
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9")
    with local_machine(tls=files) as machine, pytest.raises(requests.ConnectionError) as raised:
        ai_requests.get(machine.url + "/models", timeout=5, fingerprint=pinned)
    assert "SOCKS" in str(raised.value) and machine.asked == []
