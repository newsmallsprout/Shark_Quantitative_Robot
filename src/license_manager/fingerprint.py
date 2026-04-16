from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import uuid
from pathlib import Path

import psutil

class MachineFingerprint:
    @staticmethod
    def get_cpu_id():
        try:
            if platform.system() == "Darwin":
                return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
            elif platform.system() == "Linux":
                # Simplification for demo; real prod code reads /proc/cpuinfo or dmidecode
                with open("/proc/cpuinfo", "r") as f:
                    for line in f:
                        if "model name" in line:
                            return line.split(":")[1].strip()
            return platform.processor()
        except:
            return "UNKNOWN_CPU"

    @staticmethod
    def get_mac_address():
        try:
            for interface, snics in psutil.net_if_addrs().items():
                for snic in snics:
                    if snic.family == psutil.AF_LINK:
                        return snic.address
            return uuid.getnode()
        except:
            return "UNKNOWN_MAC"

    @staticmethod
    def get_system_uuid():
        try:
            if platform.system() == "Darwin":
                return subprocess.check_output(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]).decode().split("IOPlatformUUID")[1].split('"')[1]
            # Linux usually needs root for dmidecode, fallback to machine-id
            if os.path.exists("/etc/machine-id"):
                with open("/etc/machine-id") as f:
                    return f.read().strip()
            return str(uuid.getnode())
        except:
            return "UNKNOWN_UUID"

    @classmethod
    def get_fingerprint(cls) -> str:
        """
        Combines CPU, MAC, UUID into a SHA256 hash.
        """
        raw_data = f"{cls.get_cpu_id()}|{cls.get_mac_address()}|{cls.get_system_uuid()}"
        return hashlib.sha256(raw_data.encode()).hexdigest()

    @staticmethod
    def _read_fingerprint_override_file(path: Path) -> str | None:
        """首行非空、非 # 注释的行作为 hex fingerprint。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            line = line.strip().replace("\r", "")
            if not line or line.startswith("#"):
                continue
            return line
        return None

    @classmethod
    def _fingerprint_override_file_candidates(cls, license_key_path: str | None) -> list[Path]:
        """多路径查找，兼容 PyArmor 布局、Docker /app、以及与 license.key 同挂载目录。"""
        candidates: list[Path] = []
        if license_key_path:
            lic = Path(license_key_path)
            if not lic.is_absolute():
                lic = Path(__file__).resolve().parents[2] / lic
            candidates.append(lic.parent / "fingerprint_override")
        root = Path(__file__).resolve().parents[2]
        candidates.append(root / "license" / "fingerprint_override")
        app_root = (os.environ.get("SHARK_APP_ROOT") or "").strip()
        if app_root:
            candidates.append(Path(app_root) / "license" / "fingerprint_override")
        candidates.append(Path("/app/license/fingerprint_override"))
        seen: set[Path] = set()
        out: list[Path] = []
        for p in candidates:
            try:
                rp = p.resolve()
            except OSError:
                rp = p
            if rp not in seen:
                seen.add(rp)
                out.append(p)
        return out

    @classmethod
    def get_fingerprint_override(cls, license_key_path: str | None = None) -> str | None:
        """
        显式覆盖指纹（须与 license.key 内 machine_fingerprint 一致），供 Docker 等与签发机不一致时使用。
        优先级：环境变量 SHARK_LICENSE_FINGERPRINT → 文件 SHARK_LICENSE_FINGERPRINT_FILE →
        与 license.key 同目录的 fingerprint_override → 其它候选路径（含 /app/license）。
        license_key_path：校验器传入的 license 文件路径（与 settings 中 license_path 解析后一致）。
        """
        env = (os.environ.get("SHARK_LICENSE_FINGERPRINT") or "").strip()
        if env:
            return env
        fp_path = (os.environ.get("SHARK_LICENSE_FINGERPRINT_FILE") or "").strip()
        if fp_path:
            p = Path(fp_path)
            if p.is_file():
                v = cls._read_fingerprint_override_file(p)
                if v:
                    return v
        for path in cls._fingerprint_override_file_candidates(license_key_path):
            if path.is_file():
                v = cls._read_fingerprint_override_file(path)
                if v:
                    return v
        return None

    @classmethod
    def get_fingerprint_for_validation(cls, license_key_path: str | None = None) -> str:
        override = cls.get_fingerprint_override(license_key_path)
        if override:
            return override
        return cls.get_fingerprint()

if __name__ == "__main__":
    print(f"Fingerprint: {MachineFingerprint.get_fingerprint()}")
