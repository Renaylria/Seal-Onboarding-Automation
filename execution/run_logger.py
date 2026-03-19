"""
run_logger.py — Structured logging to Google Sheets + local files.

Provides RunLogger, a context manager that tracks a single script run:
  - Logs actions as they happen (local log file)
  - On exit, writes a summary row to the "Run Log" tab
  - On error, writes a structured row to the "Error Log" tab

Usage:
    from run_logger import RunLogger

    with RunLogger("process_applicants", log) as rl:
        rl.add_action("2 approved, 1 rejected")
        rl.set_rows_processed("3 new")
        # ... do work ...
        # errors are caught and logged automatically

    # Or log an error manually:
        rl.log_error("SLACK_003", "Playwright login failed", traceback_str)
"""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
TOKEN = ROOT / "token_gmail.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _load_sheet_id() -> str | None:
    """Read the automation log sheet ID from config.yaml."""
    try:
        with open(CONFIG) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("automation_log", {}).get("sheet_id")
    except Exception:
        return None


def _get_sheets_service():
    """Build a Google Sheets API service using token_gmail.json."""
    if not TOKEN.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        if not creds.valid:
            return None
        return build("sheets", "v4", credentials=creds)
    except Exception:
        return None


class RunLogger:
    """Context manager that tracks a script run and logs to Sheets + local file."""

    def __init__(self, script_name: str, log: logging.Logger):
        self.script_name = script_name
        self.log = log
        self.run_id = uuid.uuid4().hex[:8]
        self.start_time: float = 0
        self.actions: list[str] = []
        self.notes: list[str] = []
        self.rows_processed: str = "0"
        self.errors: list[dict] = []
        self.status: str = "SUCCESS"
        self._sheet_id = _load_sheet_id()
        self._sheets = None

    def __enter__(self):
        self.start_time = time.time()
        self.log.info("Run %s started [%s]", self.script_name, self.run_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = round(time.time() - self.start_time, 1)

        if exc_type is not None:
            self.status = "ERROR"
            tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            self.log_error("UNKNOWN", str(exc_val), tb_str, auto_fix="No")
            self.log.error("Run %s FAILED [%s]: %s", self.script_name, self.run_id, exc_val)

        self.log.info(
            "Run %s finished [%s] — %s in %.1fs",
            self.script_name, self.run_id, self.status, duration,
        )

        self._write_run_log(duration)
        self._write_error_logs()

        # Don't suppress exceptions
        return False

    def add_action(self, action: str):
        """Record an action taken during this run."""
        self.actions.append(action)

    def add_note(self, note: str):
        """Record a detailed note (e.g., name + email + reason for an action)."""
        self.notes.append(note)

    def set_rows_processed(self, summary: str):
        """Set the rows-processed summary string."""
        self.rows_processed = summary

    def set_status(self, status: str):
        """Override status (SUCCESS, ERROR, WARNING, SKIPPED)."""
        self.status = status

    def log_error(
        self,
        code: str,
        message: str,
        stack_trace: str = "",
        auto_fix: str = "No",
        resolved: str = "No",
    ):
        """Record a structured error."""
        self.errors.append({
            "code": code,
            "message": message,
            "stack_trace": stack_trace[:1000],  # Truncate for Sheets cell limit
            "auto_fix": auto_fix,
            "resolved": resolved,
        })
        if self.status != "ERROR":
            self.status = "ERROR"

    def _timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _get_sheets(self):
        """Lazy-init Sheets service."""
        if self._sheets is None:
            self._sheets = _get_sheets_service()
        return self._sheets

    def _write_run_log(self, duration: float):
        """Append a row to the Run Log tab."""
        if not self._sheet_id:
            self.log.warning("  [RunLogger] No automation_log.sheet_id in config — skipping Sheets log")
            return

        svc = self._get_sheets()
        if not svc:
            self.log.warning("  [RunLogger] Could not connect to Sheets API")
            return

        row = [
            self._timestamp(),
            self.script_name,
            self.status,
            self.rows_processed,
            "; ".join(self.actions) if self.actions else "No actions",
            str(duration),
            self.run_id,
            "\n".join(self.notes) if self.notes else "",
        ]

        try:
            result = svc.spreadsheets().values().append(
                spreadsheetId=self._sheet_id,
                range="'Run Log'!A:H",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
            # Verify by reading back the updated range
            updated_range = result.get("updates", {}).get("updatedRange", "")
            if updated_range:
                verify = svc.spreadsheets().values().get(
                    spreadsheetId=self._sheet_id, range=updated_range
                ).execute()
                if not verify.get("values"):
                    self.log.warning("  [RunLogger] Run Log write could not be verified")
        except Exception as e:
            self.log.warning("  [RunLogger] Failed to write Run Log: %s", e)

    def _write_error_logs(self):
        """Append rows to the Error Log tab."""
        if not self.errors or not self._sheet_id:
            return

        svc = self._get_sheets()
        if not svc:
            return

        rows = []
        for err in self.errors:
            rows.append([
                self._timestamp(),
                self.script_name,
                err["code"],
                err["message"],
                err["stack_trace"],
                err["auto_fix"],
                err["resolved"],
                self.run_id,
            ])

        try:
            result = svc.spreadsheets().values().append(
                spreadsheetId=self._sheet_id,
                range="'Error Log'!A:H",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            ).execute()
            # Verify by reading back
            updated_range = result.get("updates", {}).get("updatedRange", "")
            if updated_range:
                verify = svc.spreadsheets().values().get(
                    spreadsheetId=self._sheet_id, range=updated_range
                ).execute()
                if not verify.get("values"):
                    self.log.warning("  [RunLogger] Error Log write could not be verified")
        except Exception as e:
            self.log.warning("  [RunLogger] Failed to write Error Log: %s", e)
