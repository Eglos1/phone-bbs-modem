"""
SIP VoIP Client
===============
Provides a VoIP client that bridges G.711 audio between a SIP provider
and the Bell 103 modem.

Key components:

* :class:`VoIPClient` — manages SIP registration, outbound calling,
  and a background audio bridge thread.
* :class:`ProviderConfig` — dataclass for provider settings.
* :data:`PROVIDERS` — dict of preset provider configurations.
"""

from .client import VoIPClient
from .provider import ProviderConfig, PROVIDERS, lookup, list_providers, config_from_dict

__all__ = [
    "VoIPClient",
    "ProviderConfig",
    "PROVIDERS",
    "lookup",
    "list_providers",
    "config_from_dict",
]
