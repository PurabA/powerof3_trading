"""
Storage and cache management for powerof3_trading.
Organizes cached data hierarchically:
  data/cache/{date}/baselines.json
  data/cache/{date}/{strategy}/screener.json
  data/cache/{date}/{strategy}/traded_symbols.json
  data/cache/{date}/{strategy}/positions.json
  data/cache/{date}/{strategy}/orders.json
  data/cache/{date}/{strategy}/summary.json

Includes backward compatibility to automatically read legacy flat cache files:
  data/cache/{filename}_{date}.json
"""
import json
import logging
from pathlib import Path
from typing import Optional, Any
from core.timeutils import today_ist_str

logger = logging.getLogger("Storage")


def get_cache_dir(
    date_str: Optional[str] = None,
    strategy_name: Optional[str] = None,
    base_dir: Optional[Path] = None
) -> Path:
    """Returns the cache directory, creating it if it does not exist."""
    base = base_dir or Path("data/cache")
    d = date_str or today_ist_str()
    if strategy_name:
        target = base / d / strategy_name
    else:
        target = base / d
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_cache_file(
    filename: str,
    strategy_name: Optional[str] = None,
    date_str: Optional[str] = None,
    base_dir: Optional[Path] = None
) -> Path:
    """
    Resolves the file path for cache.
    First checks structured path: data/cache/{date}/{strategy}/{filename}.json (or data/cache/{date}/{filename}.json)
    Fallback: data/cache/{filename}_{date}.json (legacy flat structure)
    """
    base = base_dir or Path("data/cache")
    d = date_str or today_ist_str()

    if strategy_name:
        target_dir = base / d / strategy_name
    else:
        target_dir = base / d
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{filename}.json"

    # If the structured file doesn't exist, check for legacy flat file
    if not target_file.exists():
        legacy_file = base / f"{filename}_{d}.json"
        if legacy_file.exists():
            return legacy_file

    return target_file


def save_json_atomic(path: Path, data: Any):
    """Writes data to a JSON file atomically using a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(path)
    except Exception as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise e


def load_json_safe(path: Path, default: Any = None) -> Any:
    """Safely loads a JSON file, returning default on error or missing file."""
    if not path.exists():
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"Failed to load JSON from {path}: {e}")
        return default
