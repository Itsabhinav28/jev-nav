"""Env-driven deployment config of the streaming server (no network, WSL, Docker or API keys needed)."""
import importlib
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streaming_server"))
server = importlib.import_module("server")
slam_bridge = importlib.import_module("slam_bridge")


def test_ice_empty_by_default(monkeypatch):
    for k in ("ICE_STUN_URLS", "ICE_TURN_URL", "ICE_TURN_USER", "ICE_TURN_PASS"):
        monkeypatch.delenv(k, raising=False)
    assert server.ice_server_dicts() == []
    pc = server.make_peer_connection()
    assert not pc.__dict__.get("_RTCPeerConnection__configuration").iceServers


def test_ice_stun_and_turn(monkeypatch):
    monkeypatch.setenv("ICE_STUN_URLS", " stun:a:3478, stun:b:3478 ,")
    monkeypatch.setenv("ICE_TURN_URL", "turn:t:3478")
    monkeypatch.setenv("ICE_TURN_USER", "u")
    monkeypatch.setenv("ICE_TURN_PASS", "p")
    assert server.ice_server_dicts() == [
        {"urls": ["stun:a:3478", "stun:b:3478"]},
        {"urls": ["turn:t:3478"], "username": "u", "credential": "p"}]
    server.make_peer_connection()  # must construct


def test_slam_env_overrides(monkeypatch):
    monkeypatch.setenv("SLAM_HOST", "slam1")
    monkeypatch.setenv("SLAM_PORT", "5556")
    monkeypatch.setenv("SLAM_AUTOSTART", "0")
    monkeypatch.setenv("SLAM_WSL_DISTRO", "D")
    monkeypatch.setenv("SLAM_BIN", "/b")
    monkeypatch.setenv("SLAM_VOCAB", "/v")
    m = importlib.reload(slam_bridge)
    try:
        assert (m.HOST, m.PORT, m.AUTOSTART, m.WSL_DISTRO, m.SLAM_BIN, m.VOCAB) == ("slam1", 5556, False, "D", "/b", "/v")
        called = []
        monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: called.append(a))
        monkeypatch.setattr(m.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        assert m.start_wsl_server() is None and not called  # no WSL spawn, no crash
    finally:
        monkeypatch.undo()
        importlib.reload(slam_bridge)


def test_slam_defaults(monkeypatch):
    for k in ("SLAM_HOST", "SLAM_PORT", "SLAM_AUTOSTART", "SLAM_WSL_DISTRO", "SLAM_BIN", "SLAM_VOCAB"):
        monkeypatch.delenv(k, raising=False)
    m = importlib.reload(slam_bridge)
    assert (m.HOST, m.PORT, m.AUTOSTART, m.WSL_DISTRO) == ("127.0.0.1", 5555, True, "Ubuntu-22.04")


async def test_healthz_and_info(monkeypatch):
    monkeypatch.delenv("ICE_STUN_URLS", raising=False)
    monkeypatch.delenv("ICE_TURN_URL", raising=False)
    r = await server.healthz(make_mocked_request("GET", "/healthz"))
    assert r.status == 200 and r.text == "ok"
    app = {"phone_url": "https://x:1/phone"}
    req = make_mocked_request("GET", "/api/info", app=app)
    import json
    assert json.loads((await server.info(req)).text) == {"phone_url": "https://x:1/phone", "ice_servers": []}


def test_healthz_registered():
    import inspect
    assert '"/healthz"' in inspect.getsource(server.main)


@pytest.mark.parametrize("host", ["example.com", "10.1.2.3", "vm-1.my_domain"])
def test_make_cert_hostname_or_ip(tmp_path, monkeypatch, host):
    from cryptography import x509
    monkeypatch.setattr(server, "CERTS", tmp_path)
    crt, key = server.make_cert(host)
    assert crt.exists() and key.exists()
    san = x509.load_pem_x509_certificate(crt.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    names = [str(g.value) for g in san]
    assert host in names
    assert server.make_cert(host) == (crt, key)  # cached


def test_settings_from_process_env_only(monkeypatch, tmp_path):
    from jev_nav.config import Settings
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-env")
    monkeypatch.setenv("TYPESAFE_API_KEY", "t-env")
    s = Settings(_env_file=tmp_path / "missing.env")
    assert s.openrouter_api_key.get_secret_value() == "k-env" and s.typesafe_api_key.get_secret_value() == "t-env"
