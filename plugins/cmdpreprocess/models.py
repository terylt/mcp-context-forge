"""Models for the Linux command pre processing module."""
from pydantic import BaseModel


class Command(BaseModel):
    """The output command format after preprocessing.
    
    Attributes:
    """

    command: str = ""
    resource_type: str = ""
    name: str = ""
    exec_command: str = ""
    full_command: str = ""
    timeout: str = ""
    ops: str = ""
    replicas: int = 1
    cpu: int = 1
    memory: int = 1
    legal: bool = True
    image: str = ''
