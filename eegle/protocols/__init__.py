"""Machine-readable scientific protocol definitions."""

from eegle.protocols.attention import attention_lapse_protocol
from eegle.protocols.spec import ProtocolTarget, ScientificProtocol, load_protocol, write_protocol


__all__ = ["ProtocolTarget", "ScientificProtocol", "attention_lapse_protocol", "load_protocol", "write_protocol"]
