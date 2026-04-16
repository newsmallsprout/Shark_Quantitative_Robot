import json
import time
import base64
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

from src.license_manager.models import LicenseData
from src.license_manager.fingerprint import MachineFingerprint
from src.utils.logger import log

class LicenseValidator:
    def __init__(self, public_key_path: str, license_path: str):
        self.public_key_path = public_key_path
        self.license_path = license_path
        self._public_key = self._load_public_key()

    def _load_public_key(self):
        try:
            with open(self.public_key_path, "rb") as f:
                return serialization.load_pem_public_key(f.read())
        except Exception as e:
            log.error(f"Failed to load public key: {e}")
            raise

    def load_license(self) -> LicenseData:
        try:
            with open(self.license_path, "r") as f:
                data = json.load(f)
            return LicenseData(**data)
        except Exception as e:
            log.error(f"Failed to load license file: {e}")
            raise

    def validate(self) -> bool:
        """
        Validates the license:
        1. Signature verification
        2. Expiration check
        3. Fingerprint check
        """
        try:
            license_obj = self.load_license()
            
            # 1. Signature Verification
            signature = base64.b64decode(license_obj.signature)
            
            # Reconstruct the data payload that was signed (exclude signature field)
            payload_dict = license_obj.model_dump(exclude={'signature'})
            # Ensure consistent ordering and spacing for JSON serialization
            payload_bytes = json.dumps(payload_dict, sort_keys=True).encode()
            
            self._public_key.verify(
                signature,
                payload_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
            
            # 2. Expiration Check
            if time.time() > license_obj.expires_at:
                log.error("License expired")
                return False
            
            # 3. Fingerprint Check（Docker 等场景可用 SHARK_LICENSE_FINGERPRINT 与签发时指纹对齐）
            current_fp = MachineFingerprint.get_fingerprint_for_validation(self.license_path)
            if license_obj.machine_fingerprint != current_fp:
                log.error(f"Device fingerprint mismatch. License: {license_obj.machine_fingerprint}, Current: {current_fp}")
                return False
                
            log.info(f"License valid. Type: {license_obj.license_type}, Expires: {time.ctime(license_obj.expires_at)}")
            return True

        except InvalidSignature:
            log.error("Invalid license signature")
            return False
        except Exception as e:
            log.error(f"License validation error: {e}")
            return False
