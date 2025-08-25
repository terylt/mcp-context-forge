from pydantic import BaseModel
from typing import Optional
from typing import Optional, Dict, Any

class BaseOPAInputKeys(BaseModel):
    kind : str 
    user : str 
    tool : Dict[str, Any]
    request_ip : str 
    headers : Dict[str, str]
    response : Dict[str, str]


class OPAInput(BaseModel):
    input : BaseOPAInputKeys 


class OPAResult(BaseModel):
    allow : bool = True
    patch : Optional[Dict[str, Any]] = None
    reason: Optional[str] = None

class OPAConfig(BaseModel):
    """Configuration for the PII Filter plugin."""

    # Enable/disable detection for specific PII types
    policy: str = "None"
    server_url: str = "None"


POLICY_BUNDLE_PATH = None
POLICY_BUNDLE_URL = None
POLICY_POLL_SEC = None
POLICY_ENABLED= None