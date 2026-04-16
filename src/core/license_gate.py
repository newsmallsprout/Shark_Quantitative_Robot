"""
Runtime license gate for strategy execution and API reporting.

- Production: requires `license/public.pem` (verify) + `license/license.key` (signed payload).
- Development / CI: set `SKIP_LICENSE_CHECK=1` (see docker-compose / tests).

Obfuscation of strategy bytecode is handled separately (PyArmor); see `scripts/obfuscate_release.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.utils.logger import log


def skip_license_check() -> bool:
    return os.environ.get("SKIP_LICENSE_CHECK", "").strip().lower() in ("1", "true", "yes", "on")


def _paths() -> Tuple[Path, Path]:
    from src.core.config_manager import config_manager

    cfg = config_manager.get_config()
    root = Path(__file__).resolve().parents[2]
    lic = Path(cfg.license_path)
    if not lic.is_absolute():
        lic = root / lic
    pub = root / "license" / "public.pem"
    return pub, lic


def validate_license_detailed() -> Tuple[bool, str]:
    """
    Returns (ok, message). Does not raise; safe for FastAPI and import-time checks.
    """
    if skip_license_check():
        return True, "SKIP_LICENSE_CHECK active (development only)"

    pub, lic = _paths()
    if not pub.is_file():
        return False, "缺少 license/public.pem：请使用仓库中的公钥或向创作者索取完整发行包"
    if not lic.is_file():
        return False, f"缺少许可证文件：{lic} — 请向创作者申请 license.key 并放置于 license 目录"

    try:
        from src.license_manager.validator import LicenseValidator

        v = LicenseValidator(str(pub), str(lic))
        if v.validate():
            return True, "License OK"
        return False, "许可证校验失败：签名无效、已过期或与本机设备指纹不匹配"
    except Exception as e:
        log.error(f"license_gate: {e}")
        return False, f"许可证模块异常: {e}"


def assert_strategy_runtime_allowed() -> None:
    """Call from strategy engine import path; raises if unlicensed (unless SKIP_LICENSE_CHECK)."""
    if skip_license_check():
        return
    ok, msg = validate_license_detailed()
    if ok:
        return
    raise RuntimeError(
        f"{msg} — 策略运行时已锁定。如需本地开发请设置环境变量 SKIP_LICENSE_CHECK=1；"
        "正式使用请向创作者申请许可证。"
    )


def license_status_payload() -> Dict[str, Any]:
    """JSON-serializable status for `/api/license/status` and dashboard overlay."""
    ok, msg = validate_license_detailed()
    pub, lic = _paths()
    return {
        "license_valid": bool(ok),
        "license_locked": not bool(ok),
        "skip_license_check": skip_license_check(),
        "public_key_present": pub.is_file(),
        "license_file_present": lic.is_file(),
        "license_path": str(lic),
        "message": msg,
        "hint_zh": (
            "本终端需要创作者签发的许可证（license/license.key）与公钥 license/public.pem。"
            "若您从源码克隆运行，请联系创作者获取授权；开发者可设置 SKIP_LICENSE_CHECK=1。"
        ),
    }
