from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

import uvicorn
from core.memory.plugin import DisabledMemoryEngine


class PreviewMemoryAdmin(DisabledMemoryEngine):
    def close(self) -> None:
        return None


def _manifest(agent_root: Path) -> str:
    disabled = [
        path.name
        for path in sorted((agent_root / "plugins").iterdir())
        if path.is_dir()
    ]
    lines = [f"[plugins.{name}]\nenabled = false\n" for name in disabled]
    lines.append('[plugins."fitbit@preview"]\nenabled = true\n')
    return "\n".join(lines)


def run_preview(agent_root: Path, plugin_root: Path, host: str, port: int) -> None:
    """Run the real Akashic Dashboard shell with this plugin from a temporary install."""

    # 1. Validate both canonical sources before creating the isolated preview.
    agent_root = agent_root.resolve(strict=True)
    plugin_root = plugin_root.resolve(strict=True)
    if not (agent_root / "bootstrap" / "dashboard_api.py").is_file():
        raise FileNotFoundError(f"Akashic Agent Dashboard 不存在: {agent_root}")
    if not (plugin_root / "web_module.js").is_file():
        raise FileNotFoundError(f"Fitbit Workbench 面板不存在: {plugin_root}")

    # 2. Project the plugin into an isolated HOME and reuse the real Dashboard host.
    with tempfile.TemporaryDirectory(prefix="fitbit-dashboard-preview-") as temp:
        preview_root = Path(temp)
        plugin_home = preview_root / "home" / ".akashic-plugin"
        cache_target = plugin_home / "cache" / "preview" / "fitbit" / "dev"
        shutil.copytree(
            plugin_root,
            cache_target,
            ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "backups", "node_modules"),
        )
        plugin_home.mkdir(parents=True, exist_ok=True)
        (plugin_home / "manifest.toml").write_text(_manifest(agent_root), encoding="utf-8")
        os.environ["HOME"] = str(preview_root / "home")
        os.environ["PYTHONPATH"] = f"{agent_root}:{plugin_root}"

        import sys

        sys.path.insert(0, str(agent_root))
        sys.path.insert(0, str(plugin_root))
        from bootstrap.dashboard_api import create_dashboard_app

        app = create_dashboard_app(
            preview_root / "workspace",
            memory_admin=PreviewMemoryAdmin(),
        )
        print(f"Fitbit Dashboard 预览: http://{host}:{port}/")
        uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="在真实 Akashic Dashboard 外壳中预览 Fitbit 面板")
    parser.add_argument("--agent-root", type=Path, default=Path("/mnt/data/coding/akasic-agent"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2237)
    args = parser.parse_args()
    run_preview(args.agent_root, Path(__file__).resolve().parent.parent, args.host, args.port)


if __name__ == "__main__":
    main()
