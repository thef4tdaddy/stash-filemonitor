"""Upgrade an existing, stopped FileMonitor host installation without touching its credentials."""

import argparse
import ast
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import shutil


def upgrade(host_dir, enable_identify=False):
    host_dir = Path(host_dir).resolve()
    app = host_dir / "app"
    if not (app / "filemonitor.py").is_file() or not (host_dir / "run_monitor.py").is_file():
        raise RuntimeError("Expected the existing app/ and run_monitor.py host layout")
    with (host_dir / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Stop the running host monitor before upgrading") from exc
        source = Path(__file__).resolve().parent
        config = app / "filemonitor_config.py"
        text = config.read_text()
        marker = "# Custom host monitor options (managed by upgrade_host.py)"
        text = text.split(marker, 1)[0].rstrip() + "\n\n" + marker + "\n"
        options = {"usePolling": True, "pollIntervalSeconds": 15, "settleSeconds": 20}
        if enable_identify:
            options["identifyAfterScan"] = True
        text += f"config.update({options!r})\n"
        ast.parse(text)
        for name in ("filemonitor.py", "identify_after_scan.py"):
            ast.parse((source / name).read_text())
        backup = host_dir / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup.mkdir(parents=True)
        for name in ("filemonitor.py", "identify_after_scan.py", "filemonitor_config.py"):
            if (app / name).exists():
                shutil.copy2(app / name, backup / name)
        for name in ("filemonitor.py", "identify_after_scan.py"):
            shutil.copy2(source / name, app / name)
        config.write_text(text)
        print(f"Upgrade installed; backup: {backup}")
        print("The monitor was not started. Keep other Identify-after-scan automation disabled.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-dir", required=True, type=Path)
    parser.add_argument("--enable-identify-after-scan", action="store_true")
    args = parser.parse_args()
    upgrade(args.host_dir, args.enable_identify_after_scan)
