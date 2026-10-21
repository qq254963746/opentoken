import pytest
from typer.testing import CliRunner

import opentoken.cli.app as cli_app_module
from opentoken.cli.app import _is_loopback_host, app


def test_start_invokes_uvicorn_with_configured_bind(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    captured: dict[str, object] = {}

    def fake_run(
        app_ref: str, *, factory: bool, host: str, port: int, log_config: object = "unset"
    ) -> None:
        captured["app_ref"] = app_ref
        captured["factory"] = factory
        captured["host"] = host
        captured["port"] = port
        captured["log_config"] = log_config

    monkeypatch.setattr(cli_app_module.uvicorn, "run", fake_run)
    runner = CliRunner()

    result = runner.invoke(app, ["start"])

    assert result.exit_code == 0
    assert captured["app_ref"] == "opentoken.api.app:create_app"
    assert captured["factory"] is True
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 32117
    # We install our own handlers, so uvicorn must not overwrite them.
    assert captured["log_config"] is None
    assert (tmp_path / ".opentoken" / "config.json").exists()


def test_start_writes_a_request_log_file(monkeypatch, tmp_path) -> None:
    """`opentoken start` must leave a rotating log file with request lines.

    Regression guard: request logging used to be a no-op because nothing ever
    configured Python logging, so the root logger sat at WARNING with no
    handlers and every `logger.info("request_complete ...")` was dropped.
    """
    import logging

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli_app_module.uvicorn, "run", lambda *a, **k: None)
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["start"])

    assert result.exit_code == 0
    log_path = tmp_path / ".opentoken" / "logs" / "gateway.log"
    logging.getLogger("opentoken.api").info("request_complete request_id=abc")
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.flush()
    assert log_path.exists()
    assert "request_complete request_id=abc" in log_path.read_text(encoding="utf-8")


def test_start_log_file_none_keeps_file_logging_off(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli_app_module.uvicorn, "run", lambda *a, **k: None)
    runner = CliRunner()

    result = runner.invoke(app, ["start", "--log-file", "none"])

    assert result.exit_code == 0
    assert not (tmp_path / ".opentoken" / "logs" / "gateway.log").exists()


@pytest.mark.parametrize(
    "host,loopback",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
    ],
)
def test_is_loopback_host(host: str, loopback: bool) -> None:
    assert _is_loopback_host(host) is loopback


def test_start_warns_when_binding_non_loopback(monkeypatch, tmp_path) -> None:
    """Binding a public interface must print a stderr warning (provider sessions
    would otherwise be silently exposed beyond this machine)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli_app_module.uvicorn, "run", lambda *a, **k: None)
    runner = CliRunner()

    result = runner.invoke(app, ["start", "--host", "0.0.0.0"])

    assert result.exit_code == 0
    # CliRunner's `output` mixes stdout+stderr by default in this click version;
    # the warning text is enough — it ends up in the captured combined buffer.
    assert "exposes OpenToken" in result.output
