# backend/app/audit_ledger.py
"""Local Disk Append-Only Audit Ledger with Cryptographic Hash Chaining.

Stores audit records in local plain-text JSONL format on disk.
Each record is cryptographically linked to the previous record's SHA-256 hash,
providing tamper-evident immutable audit logs without external object storage.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER_PATH = Path("/home/deployer/netci-delivery-platform/storage/audit_ledger.jsonl")
_LEDGER_LOCK = threading.Lock()
GENESIS_HASH = "0" * 64


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append_audit_entry(
    action: str,
    actor: str,
    correlation_id: str,
    payload: dict[str, Any],
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Append a new tamper-evident record to the local disk text audit ledger."""
    path = ledger_path or DEFAULT_LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    with _LEDGER_LOCK:
        last_hash = GENESIS_HASH
        last_seq = 0

        if path.exists() and path.stat().st_size > 0:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        last_hash = record.get("hash", GENESIS_HASH)
                        last_seq = int(record.get("seq", 0))
                    except Exception:
                        pass

        seq = last_seq + 1
        now_iso = datetime.now(timezone.utc).isoformat()
        payload_canonical = _canonical_json(payload)

        # Compute tamper-evident hash chain
        hasher = hashlib.sha256()
        hasher.update(last_hash.encode("utf-8"))
        hasher.update(str(seq).encode("utf-8"))
        hasher.update(now_iso.encode("utf-8"))
        hasher.update(action.encode("utf-8"))
        hasher.update(actor.encode("utf-8"))
        hasher.update(correlation_id.encode("utf-8"))
        hasher.update(payload_canonical.encode("utf-8"))
        current_hash = hasher.hexdigest()

        entry = {
            "seq": seq,
            "timestamp": now_iso,
            "action": action,
            "actor": actor,
            "correlation_id": correlation_id,
            "payload": payload,
            "prev_hash": last_hash,
            "hash": current_hash,
        }

        # Append to disk in plain text JSONL
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return entry


def verify_audit_ledger(
    ledger_path: Path | None = None,
    expected_last_hash: str | None = None,
    expected_count: int | None = None,
    expected_genesis_hash: str | None = None,
) -> tuple[bool, str]:
    """Verify the integrity of the local disk audit ledger.
    Detects:
    1. Mid-chain payload modifications (recomputed hash mismatch).
    2. Record deletion or out-of-order records (sequence broken).
    3. Hash chain broken (prev_hash mismatch).
    4. Record truncation at the end (expected_count or expected_last_hash mismatch).
    5. Genesis rewrite from a different origin (expected_genesis_hash mismatch).

    Architecture Note:
    netCI maintains a local tamper-evident audit ledger with SHA-256 hash chaining on disk.
    This layer detects file-level tampering and unauthorized modifications. Enterprise audit
    immutability against root/disk attacks requires external hash anchoring, object lock (WORM),
    and replication.
    """
    path = ledger_path or DEFAULT_LEDGER_PATH
    if not path.exists():
        return True, "Ledger does not exist yet (clean state)"

    with _LEDGER_LOCK:
        expected_prev_hash = expected_genesis_hash or GENESIS_HASH
        expected_seq = 1

        with open(path, "r", encoding="utf-8") as f:
            for line_idx, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception as exc:
                    return False, f"Line {line_idx} contains invalid JSON: {exc}"

                seq = record.get("seq")
                prev_hash = record.get("prev_hash")
                curr_hash = record.get("hash")
                ts = record.get("timestamp", "")
                act = record.get("action", "")
                actor = record.get("actor", "")
                cid = record.get("correlation_id", "")
                payload = record.get("payload", {})

                if seq != expected_seq:
                    return False, f"Sequence broken at line {line_idx}: expected {expected_seq}, got {seq}"
                if prev_hash != expected_prev_hash:
                    return False, f"Hash chain broken at line {line_idx}: expected prev_hash {expected_prev_hash}, got {prev_hash}"

                hasher = hashlib.sha256()
                hasher.update(prev_hash.encode("utf-8"))
                hasher.update(str(seq).encode("utf-8"))
                hasher.update(ts.encode("utf-8"))
                hasher.update(act.encode("utf-8"))
                hasher.update(actor.encode("utf-8"))
                hasher.update(cid.encode("utf-8"))
                hasher.update(_canonical_json(payload).encode("utf-8"))
                recomputed_hash = hasher.hexdigest()

                if recomputed_hash != curr_hash:
                    return False, f"Tampering detected at line {line_idx}: recomputed hash {recomputed_hash} does not match recorded hash {curr_hash}"

                expected_prev_hash = curr_hash
                expected_seq += 1

        total_records = expected_seq - 1
        if expected_count is not None and total_records != expected_count:
            return False, f"Truncation detected: expected {expected_count} records, found {total_records}"

        if expected_last_hash is not None and expected_prev_hash != expected_last_hash:
            return False, f"Tail truncation or rewrite detected: expected last hash {expected_last_hash}, found {expected_prev_hash}"

        return True, f"Verified {total_records} records. Integrity intact."


def read_audit_entries(limit: int = 100, ledger_path: Path | None = None) -> list[dict[str, Any]]:
    """Read latest audit records from local text ledger."""
    path = ledger_path or DEFAULT_LEDGER_PATH
    if not path.exists():
        return []

    entries = []
    with _LEDGER_LOCK:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass

    return entries[-limit:]
