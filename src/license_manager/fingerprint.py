import hashlib
import os
import platform
import subprocess
import uuid

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

    @classmethod
    def get_fingerprint_for_validation(cls) -> str:
        """
        校验许可证时使用：若设置 SHARK_LICENSE_FINGERPRINT（如 Docker 内指纹与签发机不同），
        则与该值比对，须与 license.key 内 machine_fingerprint 字段一致。
        """
        override = (os.environ.get("SHARK_LICENSE_FINGERPRINT") or "").strip()
        if override:
            return override
        return cls.get_fingerprint()

if __name__ == "__main__":
    print(f"Fingerprint: {MachineFingerprint.get_fingerprint()}")
