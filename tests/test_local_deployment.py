from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_runners_restart_long_lived_services_without_exposing_secrets() -> None:
    api = (ROOT / "scripts/run_local_api.ps1").read_text(encoding="utf-8")
    owner = (ROOT / "scripts/run_local_sip_owner.ps1").read_text(encoding="utf-8")

    for runner, module in (
        (api, "scripts.serve_api"),
        (owner, "scripts.run_sip_owner"),
    ):
        assert "while ($true)" in runner
        assert module in runner
        assert "Start-Sleep" in runner
        assert "ALPACA_API_SECRET_KEY" not in runner


def test_local_installer_avoids_starting_duplicate_service_processes() -> None:
    installer = (ROOT / "scripts/install_local_api_task.ps1").read_text(encoding="utf-8")

    assert "ModulePattern" in installer
    assert "Get-CimInstance Win32_Process" in installer
    assert "if (-not $running)" in installer
