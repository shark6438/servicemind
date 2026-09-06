"""Typed GLPI High-Level API integration."""

from servicemind.integrations.glpi.client import GlpiAPIError, GlpiClient, GlpiClientConfig
from servicemind.integrations.glpi.models import GlpiFollowup, GlpiTicket

__all__ = [
    "GlpiAPIError",
    "GlpiClient",
    "GlpiClientConfig",
    "GlpiFollowup",
    "GlpiTicket",
]
