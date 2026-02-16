"""
Byfrost configuration - paths, defaults, and shared config helpers.

All modules import path constants from here. The ~/.byfrost/ directory
is the single location for credentials, logs, and state.
"""

from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Paths - the ~/.byfrost/ directory tree
# ---------------------------------------------------------------------------

BRIDGE_DIR = Path.home() / ".byfrost"
CERTS_DIR = BRIDGE_DIR / "certs"
LOG_DIR = BRIDGE_DIR / "logs"
SECRET_FILE = BRIDGE_DIR / "secret"
SECRET_HISTORY_FILE = BRIDGE_DIR / "secret.history"
AUTH_FILE = BRIDGE_DIR / "auth.json"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PORT = 9784
DEFAULT_SERVER_URL = "https://api.byfrost.dev"

# ---------------------------------------------------------------------------
# Shared config helpers
# ---------------------------------------------------------------------------


def source_env_file(
    path: Path,
    config: dict,
    key_map: dict[str, tuple[str, Callable]],
) -> None:
    """Parse key=value pairs from a shell-style env file.

    Args:
        path: Path to the .env file.
        config: Dict to update with parsed values.
        key_map: Mapping of ENV_VAR_NAME -> (config_key, cast_fn).
            Cast functions receive the string value and return the typed value.
    """
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            key = key.strip()
            if key in key_map:
                cfg_key, cast = key_map[key]
                try:
                    config[cfg_key] = cast(value)
                except (ValueError, TypeError):
                    pass
