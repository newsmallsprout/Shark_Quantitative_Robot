import json
import time
import base64
import uuid
import sys
import os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.license_manager.models import LicenseData, LicenseType
from src.license_manager.fingerprint import MachineFingerprint

def generate_license(private_key_path, user_id, license_type, duration_days, fingerprint=None):
    # Load Private Key
    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=None
        )

    issued_at = int(time.time())
    expires_at = issued_at + (duration_days * 24 * 3600)
    
    if fingerprint is None:
        # Defaults to current machine for testing
        fingerprint = MachineFingerprint.get_fingerprint()

    license_data = LicenseData(
        license_id=str(uuid.uuid4()),
        user_id=user_id,
        machine_fingerprint=fingerprint,
        license_type=license_type,
        issued_at=issued_at,
        expires_at=expires_at
    )
    
    # Create payload to sign
    payload_dict = license_data.model_dump(exclude={'signature'})
    payload_bytes = json.dumps(payload_dict, sort_keys=True).encode()
    
    signature = private_key.sign(
        payload_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    
    license_data.signature = base64.b64encode(signature).decode()
    
    return license_data

if __name__ == "__main__":
    # Generate keys first if not exist
    if not os.path.exists("license/private.pem"):
        print("Error: private.pem not found. Run generate_keys.py first.")
        sys.exit(1)

    # Example: Generate a trial license for THIS machine
    license_obj = generate_license(
        "license/private.pem",
        user_id="test_user_001",
        license_type=LicenseType.TRIAL,
        duration_days=3
    )
    
    with open("license/license.key", "w") as f:
        f.write(json.dumps(license_obj.model_dump(), indent=2))
        
    print("Generated license/license.key")
