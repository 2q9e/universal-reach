"""Local, non-secret configuration for public instance endpoints."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from universal_reach.errors import ReachError


def config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "UniversalReach" / "config.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "universal-reach" / "config.json"


def _validate_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReachError("invalid_config", "Configuration must be a JSON object.")
    allowed = {"lemmy_instance", "discourse_instances", "log_level"}
    unknown = set(value) - allowed
    if unknown:
        raise ReachError("invalid_config", "Configuration contains unsupported fields.", details={"fields": sorted(unknown)})
    lemmy = value.get("lemmy_instance", "https://lemmy.world")
    if not isinstance(lemmy, str):
        raise ReachError("invalid_config", "lemmy_instance must be a URL string.")
    discourse = value.get("discourse_instances", {})
    if not isinstance(discourse, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in discourse.items()):
        raise ReachError("invalid_config", "discourse_instances must map instance names to URL strings.")
    level = value.get("log_level", "WARNING")
    if not isinstance(level, str) or level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ReachError("invalid_config", "log_level must be DEBUG, INFO, WARNING, or ERROR.")
    return {"lemmy_instance": lemmy, "discourse_instances": discourse, "log_level": level.upper()}


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _validate_config({})
    except (OSError, json.JSONDecodeError) as exc:
        raise ReachError("config_read_error", "Could not read the local configuration file.") from exc
    return _validate_config(raw)


def save_config(value: dict[str, Any], path: Path | None = None) -> Path:
    target = path or config_path()
    validated = _validate_config(value)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o600)
    except OSError as exc:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)
        raise ReachError("config_write_error", "Could not write the local configuration file.") from exc
    return target
