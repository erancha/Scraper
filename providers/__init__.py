"""
Provider registry.

Import and register every Provider subclass here. The PROVIDERS dict maps
a CLI-friendly key to an instance, making it easy to add new providers:

    1. Create providers/my_new_provider.py with a Provider subclass
    2. Import it here and add an entry to PROVIDERS
"""

from .base import Provider
from .espn_nba import EspnNba

# -- Registry: add new providers here ---------------------------------------
DEFAULT_PROVIDER_KEY = "espn-nba"

PROVIDERS: dict[str, Provider] = {
    DEFAULT_PROVIDER_KEY: EspnNba(),
}
