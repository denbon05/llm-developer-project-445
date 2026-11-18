"""Names of the MCP tools the backend uses."""

from enum import StrEnum

from actions.contracts import ActionKind


class ToolName(StrEnum):
    GET_MY_PROFILE = "get_my_profile"
    GET_MY_TARIFF = "get_my_tariff"
    LIST_TARIFFS = "list_tariffs"
    LIST_MY_CHARGES = "list_my_charges"
    SEARCH_KB = "search_kb"
    PREPARE_TARIFF_CHANGE = "prepare_tariff_change"
    PREPARE_TOGGLE_SERVICE = "prepare_toggle_service"
    PREPARE_CONTACTS_UPDATE = "prepare_contacts_update"
    EXECUTE_ACTION = "execute_action"
    CREATE_TICKET = "create_ticket"


# The action each prepare tool prepares.
ACTION_BY_PREPARE_TOOL: dict[str, ActionKind] = {
    ToolName.PREPARE_TARIFF_CHANGE: ActionKind.TARIFF_CHANGE,
    ToolName.PREPARE_TOGGLE_SERVICE: ActionKind.SERVICE_TOGGLE,
    ToolName.PREPARE_CONTACTS_UPDATE: ActionKind.CONTACTS_UPDATE,
}
# A successful call of one of these creates a confirmation card.
PREPARE_TOOL_NAMES = frozenset(ACTION_BY_PREPARE_TOOL)
