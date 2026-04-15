import json
import os
import tempfile

from src.core.config_manager import config_manager
from src.darwin.experience_store import (
    append_experience_record,
    append_from_autopsy,
    tail_experience_text,
)


def test_experience_append_and_tail():
    prev = config_manager.config.darwin
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "exp.jsonl")
        config_manager.config.darwin = prev.model_copy(
            update={
                "enabled": True,
                "experience_log_path": path,
                "experience_tail_lines": 10,
            }
        )
        try:
            append_experience_record({"kind": "test", "x": 1})
            append_from_autopsy(
                {
                    "symbol": "Z/USDT",
                    "side": "long",
                    "pnl": {"realized_net": -2.5},
                    "exit": {"reason": "core_bracket_stop"},
                    "duration_sec": 12.0,
                    "entry_snapshot": {"strategy": "CoreAttack"},
                }
            )
            txt = tail_experience_text()
            lines = [json.loads(ln) for ln in txt.splitlines() if ln.strip()]
            assert len(lines) == 2
            assert lines[1]["kind"] == "close"
            assert lines[1]["exit_reason"] == "core_bracket_stop"
        finally:
            config_manager.config.darwin = prev
