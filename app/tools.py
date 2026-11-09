"""Names of the MCP tools that agents are allowed to use."""

from enum import StrEnum


class ToolName(StrEnum):
    GET_MY_PROFILE = "get_my_profile"
    GET_MY_TARIFF = "get_my_tariff"
    LIST_TARIFFS = "list_tariffs"
    LIST_MY_CHARGES = "list_my_charges"
    SEARCH_KB = "search_kb"
