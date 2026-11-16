"""Preview and single-use submission for editing one existing Qianji bill."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

import jsonschema
from mcp import types

EDIT_UPSTREAM_TOOLS = frozenset({"update_bills", "update_transfer", "update_refund"})
EDIT_TOOL_NAMES = frozenset({"preview_bill_edit", "apply_bill_edit"})
ID = {"type": "string", "pattern": "^[1-9][0-9]{0,18}$"}
MONEY = {"type": "number", "exclusiveMinimum": 0, "maximum": 9999999999.99}
NONNEGATIVE_MONEY = {"type": "number", "minimum": 0, "maximum": 9999999999.99}
PATCH_FIELDS = {
    "bookId": {"type": "string", "pattern": "^(?:-1|[1-9][0-9]{0,18})$"},
    "time": {"type": "string", "format": "date-time"},
    "money": MONEY,
    "discount": NONNEGATIVE_MONEY,
    "fee": NONNEGATIVE_MONEY,
    "categoryId": ID,
    "assetId": {"anyOf": [ID, {"type": "null"}]},
    "fromAssetId": ID,
    "targetAssetId": ID,
    "remark": {"type": "string", "maxLength": 500},
    "tagIds": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "uniqueItems": True},
    "excludeFromIncomeExpense": {"type": "boolean"},
    "excludeFromBudget": {"type": "boolean"},
    "reimbursable": {"type": "boolean"},
    "creditRepayment": {"type": "boolean"},
    "currencyConversion": {
        "type": "object", "additionalProperties": False, "minProperties": 1,
        "properties": {"sourceCurrency": {"type": "string", "minLength": 1, "maxLength": 16},
                       "targetAmount": NONNEGATIVE_MONEY, "baseAmount": NONNEGATIVE_MONEY},
    },
}
PREVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["billId", "patch"],
    "properties": {
        "billId": ID,
        "patch": {"type": "object", "additionalProperties": False, "minProperties": 1,
                  "properties": PATCH_FIELDS},
    },
}
APPLY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["planId"],
    "properties": {"planId": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
}


def result(data: dict[str, Any], error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(isError=error, structuredContent=data,
                                content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))])


class EditError(Exception):
    pass


@dataclass
class EditPlan:
    bill_id: str
    tool: str
    arguments: dict[str, Any]
    fingerprint: str
    expires: float


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_patch_values(patch: dict[str, Any]) -> None:
    # jsonschema's date-time checker is optional; validate without extra packages.
    if "time" in patch:
        value = patch["time"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise EditError("日期须包含完整时间和时区，例如 2026-11-11T07:57:00+08:00。")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise EditError("日期或时区无效。") from None
    values = [patch[key] for key in ("money", "fee", "discount") if key in patch]
    values.extend(value for key, value in patch.get("currencyConversion", {}).items() if key.endswith("Amount"))
    for value in values:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
            raise EditError("金额必须是有限数字，最多保留两位小数。")


class BillEdits:
    def __init__(self, invoke: Callable[[str, dict[str, Any]], Awaitable[types.CallToolResult]],
                 schema: Callable[[str], Awaitable[dict[str, Any]]], *,
                 clock: Callable[[], float] = time.monotonic, ttl: float = 300):
        self.invoke, self.schema, self.clock, self.ttl = invoke, schema, clock, ttl
        self.plans: dict[str, EditPlan] = {}
        self.lock = asyncio.Lock()

    @staticmethod
    def tools() -> list[types.Tool]:
        return [
            types.Tool(name="preview_bill_edit", description=(
                "修改已有账单前先调用。一次预览一笔普通收支、待报销支出、转账/信用卡还款或退款的字段修改；"
                "返回原记录、逐字段变更和五分钟有效的 planId，不提交修改。先查询真实 billId，分类、资产和标签 ID 不得猜测。"
                "patch 支持该账单类型在上游允许的金额、日期、备注、分类、资产、账本、标签等字段；不支持新增或删除。"
                "必须向用户展示变更，得到本次确认后才调用 apply_bill_edit。"),
                inputSchema=PREVIEW_SCHEMA,
                annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                  idempotentHint=False, openWorldHint=False)),
            types.Tool(name="apply_bill_edit", description=(
                "保存一笔已经预览、并经用户对本次具体变更确认的账单编辑。"
                "这是覆盖已有财务记录的写入操作，必须让 ChatGPT 请求本次批准，不要记住批准。"
                "只接受 preview_bill_edit 返回的 planId；原记录变化、预览过期或重复提交会拒绝。"
                "失败或结果不确定时不得自动重试，应刷新并核对账单。"),
                inputSchema=APPLY_SCHEMA,
                annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                                  idempotentHint=False, openWorldHint=False)),
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if name not in EDIT_TOOL_NAMES:
                raise EditError("未知的编辑操作。")
            schema = PREVIEW_SCHEMA if name == "preview_bill_edit" else APPLY_SCHEMA
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(arguments)
            if name == "preview_bill_edit":
                validate_patch_values(arguments["patch"])
            async with asyncio.timeout(105):
                if name == "preview_bill_edit":
                    return await self.preview(arguments["billId"], arguments["patch"])
                return await self.apply(arguments["planId"])
        except (jsonschema.ValidationError, EditError) as error:
            message = str(error) if isinstance(error, EditError) else "编辑参数无效，请核对账单 ID、字段、金额和日期。"
            return result({"saved": False, "message": message}, True)
        except Exception:
            if name == "apply_bill_edit":
                return result({"saved": None, "message": "提交状态无法确认，可能已生效。请刷新并核对账单，不要自动重试。"}, True)
            return result({"saved": False, "message": "编辑准备失败，未提交修改；请检查连接后重新预览。"}, True)

    async def checked_call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        response = await self.invoke(name, arguments)
        if response.isError:
            raise EditError("钱迹读取或同步失败，未提交修改；请恢复连接后重新预览。")
        return response

    @staticmethod
    def payload(response: types.CallToolResult) -> Any:
        data = response.structuredContent
        if data is not None:
            return data.get("result", data)
        for content in response.content:
            if isinstance(content, types.TextContent):
                try:
                    return json.loads(content.text)
                except ValueError:
                    pass
        raise EditError("上游未返回可核对的账单，已停止编辑。")

    async def read_bill(self, bill_id: str) -> dict[str, Any]:
        data = self.payload(await self.checked_call("get_bills", {"billIds": [bill_id]}))
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict) or data[0].get("id") != bill_id:
            raise EditError("没有唯一匹配的账单，已停止编辑。")
        return data[0]

    @staticmethod
    def before_value(bill: dict[str, Any], field: str) -> Any:
        if field == "reimbursable":
            return bill.get("type") == 5
        if field == "creditRepayment":
            return bill.get("type") == 3
        if field == "money" and isinstance(bill.get("currencyConversion"), dict):
            return bill["currencyConversion"].get("sourceAmount", bill.get("money"))
        return bill.get({"fromAssetId": "fromId", "targetAssetId": "targetId"}.get(field, field))

    async def preview(self, bill_id: str, patch: dict[str, Any]) -> types.CallToolResult:
        patch = copy.deepcopy(patch)
        await self.checked_call("refresh_cache", {})
        before = await self.read_bill(bill_id)
        kind = before.get("type")
        if kind in (0, 1, 5):
            tool, args = "update_bills", {"updates": [{"billId": bill_id, "patch": patch}]}
        elif kind in (2, 3):
            # Upstream clears the other adjustment even when only one is sent.
            # Include that effect explicitly in both the preview and saved patch.
            if "fee" in patch or "discount" in patch:
                patch.setdefault("fee", 0)
                patch.setdefault("discount", 0)
            if patch.get("fee", 0) > 0 and patch.get("discount", 0) > 0:
                raise EditError("手续费和优惠不能同时大于零。")
            tool, args = "update_transfer", {"billId": bill_id, "patch": patch}
        elif kind == 20:
            tool, args = "update_refund", {"refundBillId": bill_id, "patch": patch}
        else:
            raise EditError("该账单类型没有受支持的编辑接口，请在钱迹 App 中处理；不会删除后重建。")
        amount = patch.get("money", self.before_value(before, "money"))
        for field in ("discount", "fee"):
            if patch.get(field, before.get(field, 0)) > amount:
                raise EditError("优惠或手续费不能大于调整后的账单金额。")
        jsonschema.validate(args, await self.schema(tool))
        changes = [{"field": field, "before": self.before_value(before, field), "after": value}
                   for field, value in patch.items() if self.before_value(before, field) != value]
        if not changes:
            return result({"saved": False, "billId": bill_id, "changes": [], "message": "请求的字段已经是这些值，无需修改。"})
        if len(json.dumps(before, ensure_ascii=False).encode()) > 65536:
            raise EditError("账单记录过大，无法安全预览。")
        self.plans = {key: plan for key, plan in self.plans.items() if plan.expires > self.clock()}
        if len(self.plans) >= 20:
            raise EditError("待确认预览过多，请等现有预览过期后重试。")
        plan_id = secrets.token_hex(32)
        self.plans[plan_id] = EditPlan(bill_id, tool, copy.deepcopy(args), fingerprint(before), self.clock() + self.ttl)
        return result({"saved": False, "planId": plan_id, "billId": bill_id, "currentBill": before,
                       "changes": changes, "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(),
                       "message": "仅预览，尚未修改。请展示上述变更并由用户确认；保存会同步至钱迹，可能影响关联资产余额。"})

    async def apply(self, plan_id: str) -> types.CallToolResult:
        async with self.lock:
            plan = self.plans.pop(plan_id, None)
            if plan is None or plan.expires <= self.clock():
                raise EditError("预览不存在、已使用或已过期，请重新预览并确认，不能重复提交。")
            await self.checked_call("refresh_cache", {})
            current = await self.read_bill(plan.bill_id)
            if fingerprint(current) != plan.fingerprint:
                raise EditError("账单在预览后已变化，未提交修改。请读取最新记录，重新预览并确认。")
            # Consume the plan before the request; never retry an uncertain write.
            try:
                response = await self.invoke(plan.tool, copy.deepcopy(plan.arguments))
            except Exception:
                return result({"saved": None, "message": "提交结果不确定，可能已生效。该预览已失效；请刷新并核对账单，不要重试此提交。"}, True)
            if response.isError:
                return result({"saved": None, "message": "钱迹未确认修改成功，可能已部分或全部生效。该预览已失效；请刷新并核对，不要自动重试。"}, True)
            try:
                data = self.payload(response)
            except EditError:
                return result({"saved": None, "message": "上游响应无法解析，可能已生效。请刷新并核对账单，不要自动重试。"}, True)
            record = data[0] if isinstance(data, list) and len(data) == 1 else data
            if not isinstance(record, dict) or record.get("id") != plan.bill_id:
                return result({"saved": None, "message": "上游返回的修改记录无法核对，可能已生效。请刷新并检查，不要自动重试。"}, True)
            return result({"saved": True, "billId": plan.bill_id, "record": record,
                           "message": "上游已返回修改并同步确认的记录。请核对返回值。"})
