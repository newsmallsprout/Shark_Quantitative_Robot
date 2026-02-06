from pydantic import BaseModel
from typing import Optional
from enum import Enum

class LicenseType(str, Enum):
    TRIAL = "trial"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEAR = "half_year"
    YEARLY = "yearly"
    LIFETIME = "lifetime"

class LicenseData(BaseModel):
    license_id: str
    user_id: str
    machine_fingerprint: str
    license_type: LicenseType
    issued_at: int
    expires_at: int
    signature: Optional[str] = None
