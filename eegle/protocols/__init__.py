"""Machine-readable scientific protocol definitions."""

from eegle.protocols.attention import attention_lapse_protocol
from eegle.protocols.spec import ProtocolTarget, ScientificProtocol, load_protocol, write_protocol
from eegle.protocols.study1 import study1_protocol


__all__ = [
    "ProtocolTarget",
    "ScientificProtocol",
    "attention_lapse_protocol",
    "study1_protocol",
    "load_protocol",
    "write_protocol",
]
