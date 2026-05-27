"""
IG REST and streaming API layer.

Public surface:
    - :class:`~ig_api.rest_client.IGRestClient`
    - :class:`~ig_api.streaming_client.IGStreamingClient`
    - :mod:`ig_api.exceptions`
"""

from ig_api.exceptions import IGAPIError, IGAuthError, IGOrderError, IGStreamError
from ig_api.rest_client import IGRestClient
from ig_api.streaming_client import IGStreamingClient

__all__ = [
    "IGAPIError",
    "IGAuthError",
    "IGOrderError",
    "IGStreamError",
    "IGRestClient",
    "IGStreamingClient",
]
