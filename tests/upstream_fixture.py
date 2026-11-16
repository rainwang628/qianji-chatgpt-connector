"""Fake local Qianji MCP. No Qianji network traffic or real financial data."""

import logging
import os
from pathlib import Path
from typing import Annotated

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse

logging.disable(logging.CRITICAL)
pat = os.environ["FIXTURE_PAT"]
port = int(os.environ["FIXTURE_PORT"])
marker = Path(os.environ["FIXTURE_WRITE_MARKER"])
mcp = FastMCP("fictional-qianji", stateless_http=True, json_response=True)
editable_bill = {"id": "9002", "bookId": "101", "type": 0, "money": 12,
                 "remark": "虚构晚餐", "updateTime": "2026-11-11T10:00:00+08:00"}
batch_bills = {str(i): {"id": str(i), "bookId": "101", "assetId": "700", "type": 0,
                         "time": "2026-01-02T10:00:00+08:00", "money": i,
                         "remark": "虚构批量账单", "categoryId": "200", "tagIds": [],
                         "excludeFromIncomeExpense": False, "excludeFromBudget": False, "discount": 0}
               for i in (9003, 9004, 9005, 9006)}
fixture_assets = [{"id": "700", "name": "虚构原账户", "currency": "CNY", "status": 0, "inCount": 1, "money": 50000},
                  {"id": "701", "name": "虚构新账户", "currency": "CNY", "status": 0, "inCount": 1, "money": 50000}]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_books(includeHidden: bool = False) -> dict[str, object]:
    """Return fictional books."""
    return {"books": [{"id": "101", "name": "虚构测试账本"}],
            "result": [{"bookId": "101", "name": "虚构测试账本"}]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_assets(includeHidden: bool = False, includeBalances: bool = False) -> dict[str, object]:
    children = [dict(asset) for asset in fixture_assets]
    if not includeBalances:
        for asset in children:
            asset.pop("money", None)
    return {"result": [{"name": "虚构分组", "children": children}]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_user_info() -> dict[str, object]:
    return {"baseCurrency": "CNY"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_categories(bookId: str) -> dict[str, object]:
    return {"result": [{"id": "200", "name": "虚构旧分类", "type": 0, "children": [
        {"id": "201", "name": "虚构新分类", "type": 0}]}]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_bills(
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    remarkKeyword: str | None = None,
) -> dict[str, object]:
    """Return fictional bills."""
    if remarkKeyword == "fail":
        raise RuntimeError("upstream failure includes " + pat)
    if remarkKeyword == "leak":
        return {"accidental_debug": pat}
    return {"bills": [{"id": "9001", "money": "35.00", "currency": "CNY",
                       "remark": "虚构午餐"}], "nextCursor": None, "limit": limit}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def connect_qianji() -> dict[str, object]:
    """Create a fictional connection URL."""
    return {"url": "http://127.0.0.1:3000/connect?ticket=fictional_ticket",
            "message": "仅为测试链接，没有真实账号。"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def refresh_cache() -> dict[str, object]:
    """Refresh a fictional cache, without writing financial data."""
    return {"billCount": 1}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_bills(billIds: list[str]) -> dict[str, object]:
    """Get a fictional editable bill; no account or real data is used."""
    bills = {"9002": editable_bill, **batch_bills}
    return {"result": [dict(bills[bill_id]) for bill_id in billIds if bill_id in bills]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def update_bills(updates: list[dict]) -> dict[str, object]:
    """Edit only the isolated fictional fixture record."""
    if updates and all(u.get("billId") in batch_bills for u in updates):
        batch_marker = Path(str(marker) + "-batch")
        batch_marker.write_text(str(len(updates)), encoding="utf-8")
        for update in updates:
            old = batch_bills[update["billId"]]["assetId"]
            new = update["patch"].get("assetId", old)
            if old != new:
                for asset in fixture_assets:
                    if asset["id"] == old:
                        asset["money"] += batch_bills[update["billId"]]["money"]
                    if asset["id"] == new:
                        asset["money"] -= batch_bills[update["billId"]]["money"]
            batch_bills[update["billId"]].update(update["patch"])
        return {"result": [dict(batch_bills[u["billId"]]) for u in updates]}
    if len(updates) != 1 or updates[0].get("billId") != "9002":
        raise ValueError("unknown fictional bill")
    editable_bill.update(updates[0]["patch"])
    editable_bill["updateTime"] = "2026-11-11T10:01:00+08:00"
    return {"result": [dict(editable_bill)]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def create_bills() -> dict[str, object]:
    """A write tool that the bridge must never expose or invoke."""
    marker.write_text("write_was_called", encoding="utf-8")
    return {"created": True}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def execute_sql() -> dict[str, object]:
    """An unknown tool, deliberately mislabeled read-only."""
    marker.write_text("unknown_tool_was_called", encoding="utf-8")
    return {"executed": True}


class RequirePat:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if headers.get(b"authorization") != f"Bearer {pat}".encode():
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(RequirePat(mcp.streamable_http_app()), host="127.0.0.1", port=port,
                log_level="critical", access_log=False)
