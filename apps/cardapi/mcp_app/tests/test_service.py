from __future__ import annotations

import json

import pytest
from apps.cardapi.mcp_app import service
from starlette.requests import Request

_EXPECTED_TOOLS = {
    "get_all_decline_codes",
    "get_decline_codes_metadata",
    "lookup_decline_code",
    "search_decline_codes",
}


@pytest.mark.asyncio
async def test_fastmcp_tools_and_health_inventory() -> None:
    tools = await service._list_registered_tools()

    assert set(tools) == _EXPECTED_TOOLS
    assert service.mcp.name == "card-decline-codes"

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "root_path": "",
        }
    )
    response = await service.health_check(request)
    payload = json.loads(response.body)

    assert payload == {
        "status": "healthy",
        "tools_count": 4,
        "tool_names": list(tools),
    }