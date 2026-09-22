"""
audit_chain.py
Cryptographically tamper-evident hash-chained audit logging service.
Uses SHA-256 chaining to record admin actions immutably and verify chain integrity.
"""

import hashlib
import json
import os
from datetime import datetime
from typing import Dict, Any, List, Tuple

GENESIS_HASH = "0" * 64
AUDIT_FALLBACK_FILE = "audit_chain_local.json"


def compute_block_hash(
    index: int,
    timestamp: str,
    admin: str,
    action: str,
    details_str: str,
    prev_hash: str
) -> str:
    """Computes deterministic SHA-256 hash for a block payload."""
    payload = f"{index}|{timestamp}|{admin}|{action}|{details_str}|{prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_firestore_db():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        import streamlit as st

        if not firebase_admin._apps:
            key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase_key.json")
            if hasattr(st, "secrets") and "FIREBASE_KEY" in st.secrets:
                key_dict = json.loads(st.secrets["FIREBASE_KEY"])
                cred = credentials.Certificate(key_dict)
            else:
                cred = credentials.Certificate(key_path)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception:
        return None


def get_audit_chain() -> List[Dict[str, Any]]:
    """Retrieves all audit blocks ordered chronologically by index."""
    # Check local chain first for zero-latency execution
    if os.path.exists(AUDIT_FALLBACK_FILE):
        try:
            with open(AUDIT_FALLBACK_FILE, "r", encoding="utf-8") as f:
                chain = json.load(f)
                if chain:
                    return chain
        except Exception:
            pass

    # Remote fallback if local is not present
    try:
        db = _get_firestore_db()
        if db:
            docs = (
                db.collection("audit_chain")
                .order_by("index", direction=firestore.Query.ASCENDING)
                .stream()
            )
            chain = [d.to_dict() for d in docs]
            if chain:
                _save_audit_chain_local(chain)
                return chain
    except Exception:
        pass

    return []


def _save_audit_chain_local(chain: List[Dict[str, Any]]):
    try:
        with open(AUDIT_FALLBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(chain, f, indent=2)
    except Exception:
        pass


def record_audit_event(admin: str, action: str, details: Any = None) -> Dict[str, Any]:
    """
    Appends a new cryptographically chained audit event.
    """
    if details is None:
        details = {}
    details_str = json.dumps(details, sort_keys=True)

    current_chain = get_audit_chain()
    new_index = len(current_chain)
    prev_hash = current_chain[-1]["hash"] if current_chain else GENESIS_HASH
    timestamp = datetime.utcnow().isoformat()

    block_hash = compute_block_hash(
        index=new_index,
        timestamp=timestamp,
        admin=admin,
        action=action,
        details_str=details_str,
        prev_hash=prev_hash
    )

    block = {
        "index": new_index,
        "timestamp": timestamp,
        "admin": admin,
        "action": action,
        "details": details,
        "details_str": details_str,
        "prev_hash": prev_hash,
        "hash": block_hash
    }

    # Store in Firestore
    db = _get_firestore_db()
    saved_remote = False
    if db:
        try:
            db.collection("audit_chain").document(f"block_{new_index:06d}").set(block)
            saved_remote = True
        except Exception:
            pass

    # Always persist locally too as backup
    current_chain.append(block)
    _save_audit_chain_local(current_chain)

    return block


def verify_chain_integrity(chain: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Verifies the cryptographic validity of the entire audit chain.
    Checks parent-hash links and SHA-256 payload integrity.
    """
    if chain is None:
        chain = get_audit_chain()

    if not chain:
        return {
            "is_valid": True,
            "total_blocks": 0,
            "tampered_indices": [],
            "message": "Audit chain is empty. Genesis block will be created on first admin action."
        }

    tampered_indices = []
    for i, block in enumerate(chain):
        # 1. Check index order
        if block.get("index") != i:
            tampered_indices.append(i)
            continue

        # 2. Check previous hash connection
        expected_prev = GENESIS_HASH if i == 0 else chain[i - 1]["hash"]
        if block.get("prev_hash") != expected_prev:
            tampered_indices.append(i)
            continue

        # 3. Check current block hash calculation
        details_str = block.get("details_str")
        if details_str is None:
            details_str = json.dumps(block.get("details", {}), sort_keys=True)

        calculated_hash = compute_block_hash(
            index=block.get("index", i),
            timestamp=block.get("timestamp", ""),
            admin=block.get("admin", ""),
            action=block.get("action", ""),
            details_str=details_str,
            prev_hash=block.get("prev_hash", "")
        )

        if calculated_hash != block.get("hash"):
            tampered_indices.append(i)

    is_valid = len(tampered_indices) == 0
    message = (
        f"Chain Verified: All {len(chain)} blocks are cryptographically valid and tamper-evident ✅"
        if is_valid
        else f"TAMPERING DETECTED: Discrepancy found at block index(es): {tampered_indices} 🚨"
    )

    return {
        "is_valid": is_valid,
        "total_blocks": len(chain),
        "tampered_indices": tampered_indices,
        "message": message
    }
