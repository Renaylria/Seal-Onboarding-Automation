"""
tui_status.py — Write status JSON files consumed by the sudoku-blueprint TUI.

The TUI's ToolsPanel reads two files from ~/Projects/sudoku-blueprint/:
  - onboarding_status.json  (persistent run history, stats, event log)
  - onboarding_live.json    (ephemeral phase/step indicator)

This module provides helpers so that the V1 automation scripts can update
the TUI in real time as they process applicants, challenges, and cleanup.
"""

import json
import os
from datetime import datetime
from pathlib import Path

STATUS_PATH = Path(os.environ.get("HOME", "/tmp")) / "Projects" / "sudoku-blueprint" / "onboarding_status.json"
LIVE_PATH   = Path(os.environ.get("HOME", "/tmp")) / "Projects" / "sudoku-blueprint" / "onboarding_live.json"

MAX_HISTORY = 200
MAX_RUN_LOG = 50


def _load_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "last_run": None,
            "total_runs": 0,
            "total_added": 0,
            "total_removed": 0,
            "pending_add": 0,
            "pending_remove": 0,
            "last_result": None,
            "history": [],
            "run_log": [],
            "updated_at": None,
        }


def _save_status(data: dict):
    data["updated_at"] = datetime.now().isoformat()
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # TUI status is optional — don't crash if path is unavailable


def set_live(phase: str, detail: str = "", email: str = "", step: str = ""):
    """Update the live status indicator (phase spinner in TUI)."""
    try:
        LIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_PATH.write_text(json.dumps({
            "phase": phase,
            "detail": detail,
            "email": email,
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "pid": os.getpid(),
        }), encoding="utf-8")
    except OSError:
        pass  # TUI status is optional — don't crash if path is unavailable


def log_run_start(script_name: str):
    """Record that a script run has started."""
    data = _load_status()
    data["total_runs"] = data.get("total_runs", 0) + 1
    data["last_run"] = datetime.now().isoformat()
    run_log = data.get("run_log", [])
    run_log.append({
        "time": datetime.now().isoformat(),
        "msg": f"Run #{data['total_runs']} started ({script_name})",
        "level": "info",
    })
    data["run_log"] = run_log[-MAX_RUN_LOG:]
    _save_status(data)


def log_run_msg(msg: str, level: str = "info"):
    """Append a message to the run log."""
    data = _load_status()
    run_log = data.get("run_log", [])
    run_log.append({
        "time": datetime.now().isoformat(),
        "msg": msg,
        "level": level,
    })
    data["run_log"] = run_log[-MAX_RUN_LOG:]
    _save_status(data)


def log_event(action: str, email: str, result: str,
              slack: str = "", name: str = "", reason: str = "",
              verify_group: bool = None, verify_slack: bool = None):
    """Record an onboarding/offboarding event in history."""
    data = _load_status()
    event = {
        "action": action,
        "email": email,
        "result": result,
        "slack": slack or None,
        "time": datetime.now().isoformat(),
    }
    if name:
        event["name"] = name
    if reason:
        event["reason"] = reason
    if verify_group is not None:
        event["verify_group"] = verify_group
    if verify_slack is not None:
        event["verify_slack"] = verify_slack

    if action == "add":
        data["total_added"] = data.get("total_added", 0) + 1
    elif action == "remove":
        data["total_removed"] = data.get("total_removed", 0) + 1

    history = data.get("history", [])
    history.append(event)
    data["history"] = history[-MAX_HISTORY:]
    _save_status(data)


def log_result(result: str):
    """Set the last_result field."""
    data = _load_status()
    data["last_result"] = result
    _save_status(data)
