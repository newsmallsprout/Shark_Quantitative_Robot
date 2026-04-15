"""
Runtime string indirection for IPC / control-plane identifiers.

Plain literals are not stored in-repo; release pipelines may additionally
wrap strategy modules with PyArmor (see scripts/obfuscate_release.py).
"""

from __future__ import annotations

_OBF_KEY = b"SharkQ\x01"


def _zk(hex_payload: str) -> str:
    raw = bytes.fromhex(hex_payload)
    k = _OBF_KEY
    return bytes(c ^ k[i % len(k)] for i, c in enumerate(raw)).decode()


# ZeroMQ PUB/SUB topics (must match publisher and subscriber)
IPC_TOPIC_AI_SCORE = _zk("12213e21281e5316")
IPC_TOPIC_L1_TUNING = _zk("1f593e263e1f481d2f")
IPC_TOPIC_L2_SYMBOLS = _zk("1f5a3e21321c431c2432")

IPC_SUBSCRIBE_DEFAULT_TOPICS: list[str] = [
    IPC_TOPIC_AI_SCORE,
    IPC_TOPIC_L1_TUNING,
    IPC_TOPIC_L2_SYMBOLS,
]
