"""
VoIP Provider Configuration Presets
====================================
Pre-configured settings for popular SIP VoIP providers that can be used
to place outbound calls to PSTN phone numbers or BBSes.

Each provider requires a user account and credentials obtained directly
from the provider.  Free / unlimited options are noted where available.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderConfig:
    """Configuration for a SIP VoIP provider."""

    name: str
    """Human-readable provider name."""

    server: str
    """SIP server hostname or IP."""

    port: int = 5060
    """SIP server port (default 5060)."""

    description: str = ""
    """Additional notes about this provider."""

    requires_register: bool = True
    """Whether the client must REGISTER with this provider before calling."""

    dtmf_mode: str = "inband"
    """DTMF transport: 'inband' (audio tones) or 'rfc2833' (telephone-event)."""


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, ProviderConfig] = {
    "voipms": ProviderConfig(
        name="VoIP.ms",
        server="atlanta.voip.ms",
        port=5060,
        description=(
            "Cheapest pay-per-use option (~$0.01/min US/Canada).  "
            "No monthly fee, ~$15 minimum deposit.  Create a sub-account "
            "in the VoIP.ms portal for SIP credentials."
        ),
        requires_register=True,
    ),
    "callcentric": ProviderConfig(
        name="Callcentric",
        server="callcentric.com",
        port=5060,
        description=(
            "Pay-per-use provider.  Offers a pay-per-call plan with "
            "no monthly fee.  SIP credentials from your Callcentric dashboard."
        ),
        requires_register=True,
    ),
    "sip_us": ProviderConfig(
        name="SIP.US",
        server="sip.sip.us",
        port=5060,
        description=(
            "Pay-per-use provider.  Requires account creation at sip.us "
            "to obtain SIP credentials."
        ),
        requires_register=True,
    ),
    "generic": ProviderConfig(
        name="Generic SIP Provider",
        server="",
        port=5060,
        description=(
            "Generic SIP provider.  Fill in server, username, and password "
            "from your provider's documentation."
        ),
        requires_register=True,
    ),
}


def lookup(name: str) -> ProviderConfig:
    """Look up a provider preset by key name.  Raises KeyError if not found."""
    if name in PROVIDERS:
        return PROVIDERS[name]
    raise KeyError(
        f"Unknown provider '{name}'.  Available: {', '.join(PROVIDERS)}"
    )


def list_providers() -> None:
    """Print available provider presets."""
    print("Available VoIP provider presets:")
    print("=" * 60)
    for key, cfg in PROVIDERS.items():
        print(f"  {key}")
        print(f"    {cfg.name}")
        if cfg.description:
            print(f"    {cfg.description}")
        print()


def config_from_dict(d: dict) -> ProviderConfig:
    """Create a ProviderConfig from a dictionary (e.g. loaded from JSON)."""
    return ProviderConfig(**{k: v for k, v in d.items() if k in ProviderConfig.__dataclass_fields__})
