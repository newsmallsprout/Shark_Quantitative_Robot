import platform
import uuid
import hashlib
import subprocess
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

if __name__ == "__main__":
    print(f"Fingerprint: {MachineFingerprint.get_fingerprint()}")
