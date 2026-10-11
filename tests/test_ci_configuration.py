"""CI 支持范围契约，只检查项目配置，不请求 GitHub。"""

from pathlib import Path


def test_ci_runs_windows_and_supported_python_with_format_gate():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "windows-latest" in workflow, "需在 Windows 运行离线回归"
    assert "matrix" in workflow
    assert "3.11" in workflow
    assert "3.13" in workflow
    assert "ruff format --check" in workflow, "CI 缺少格式检查门槛"
