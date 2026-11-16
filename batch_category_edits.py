"""Explicit, one-use previews for bounded category and account edits."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema
from mcp import types

from bill_edits import EditError, ID, fingerprint, result


BATCH_TOOL_NAMES = frozenset({"preview_batch_categories", "apply_batch_categories"})
MAX_BATCH_SIZE = 10
REVIEW_PROPERTIES = {
    "billId": ID,
    "bookId": {"type": "string"}, "bookName": {"type": "string"},
    "assetId": ID, "assetName": {"type": "string"}, "currency": {"type": "string", "minLength": 1},
    "time": {"type": "string"}, "type": {"type": "integer", "enum": [0, 1]},
    "money": {"type": "number"}, "remark": {"type": "string"},
    "oldCategoryId": ID, "oldCategoryName": {"type": "string"},
    "newCategoryId": ID, "newCategoryName": {"type": "string"},
}
REVIEW_REQUIRED = list(REVIEW_PROPERTIES)
REVIEW_PROPERTIES.update({"newAssetId": ID, "newAssetName": {"type": "string"}})
REVIEW_ITEM = {"type": "object", "additionalProperties": False,
               "required": REVIEW_REQUIRED, "properties": REVIEW_PROPERTIES}
BALANCE_ITEM = {
    "type": "object", "additionalProperties": False,
    "required": ["assetId", "assetName", "currency", "before", "delta", "after"],
    "properties": {"assetId": ID, "assetName": {"type": "string"}, "currency": {"const": "CNY"},
                   "before": {"type": "string"}, "delta": {"type": "string"}, "after": {"type": "string"}},
}
PREVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["updates"],
    "properties": {"updates": {"type": "array", "minItems": 1, "maxItems": MAX_BATCH_SIZE,
        "items": {"type": "object", "additionalProperties": False, "required": ["billId", "categoryId"],
                  "properties": {"billId": ID, "categoryId": ID, "assetId": ID}}}},
}
APPLY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["planId", "reviewed"],
    "properties": {"planId": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                   "reviewed": {"type": "array", "minItems": 1, "maxItems": MAX_BATCH_SIZE,
                                "items": REVIEW_ITEM},
                   "balancePreview": {"type": "array", "maxItems": 2, "items": BALANCE_ITEM}},
}


@dataclass
class BatchPlan:
    reviewed: list[dict[str, Any]]
    before: dict[str, dict[str, Any]]
    balance_preview: list[dict[str, Any]]
    expires: float


def cents(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise EditError("金额或账户余额不可核对。")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
            raise EditError("金额或账户余额不是有效的两位小数。")
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise EditError("金额或账户余额无效。") from None


class PrivateAudit:
    """Append-only recovery clues, written before a financial request is sent."""

    def __init__(self, directory: Path):
        self.directory = directory

    def append(self, entry: dict[str, Any]) -> None:
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        dir_fd = os.open(self.directory, dir_flags)
        try:
            mode = os.fstat(dir_fd).st_mode
            if not stat.S_ISDIR(mode) or mode & 0o077:
                raise OSError("private directory permissions are too broad")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open("batch-category-audit.jsonl", flags, 0o600, dir_fd=dir_fd)
            try:
                os.fchmod(fd, 0o600)
                data = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                written = os.write(fd, data)
                if written != len(data):
                    raise OSError("incomplete audit entry")
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)


class BatchCategoryEdits:
    def __init__(self, invoke: Callable[[str, dict[str, Any]], Awaitable[types.CallToolResult]],
                 schema: Callable[[str], Awaitable[dict[str, Any]]], audit: PrivateAudit,
                 *, clock: Callable[[], float] = time.monotonic, ttl: float = 300,
                 lock: asyncio.Lock | None = None):
        self.invoke, self.schema, self.audit = invoke, schema, audit
        self.clock, self.ttl = clock, ttl
        self.lock = lock or asyncio.Lock()
        self.plans: dict[str, BatchPlan] = {}

    @staticmethod
    def tools() -> list[types.Tool]:
        return [
            types.Tool(name="preview_batch_categories", inputSchema=PREVIEW_SCHEMA, description=(
                "一次从缓存预览最多十笔普通收支的分类修改；可选同时修改普通支出的账户，仅支持同一来源及目标人民币账户、无优惠和跨币种。"
                "展示每笔原值、新值及账户余额预计变化；须使用真实 ID，用户批准整个具体批次后才能提交。"),
                annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                  idempotentHint=False, openWorldHint=False)),
            types.Tool(name="apply_batch_categories", inputSchema=APPLY_SCHEMA, description=(
                "一次提交已预览并经用户明确确认的整批分类及可选账户修改。调用必须触发本次宿主批准，不记住批准。"
                "原样提供预览返回的 planId、reviewed 和 balancePreview；服务端核对全部记录、余额及已冻结的变更。"
                "预览过期、被使用、任一记录变化或审批内容不符时拒绝整批；结果不确定时不得自动重试。"),
                annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                                  idempotentHint=False, openWorldHint=True)),
        ]

    @staticmethod
    def payload(response: types.CallToolResult) -> Any:
        if response.isError:
            raise EditError("读取或同步失败，未开始本次写入。")
        if response.structuredContent is not None:
            return response.structuredContent.get("result", response.structuredContent)
        for item in response.content:
            if isinstance(item, types.TextContent):
                try:
                    return json.loads(item.text)
                except ValueError:
                    continue
        raise EditError("上游未返回可核对的数据。")

    async def checked(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.payload(await self.invoke(name, arguments))

    async def read_records(self, updates: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        ids = [item["billId"] for item in updates]
        data = await self.checked("get_bills", {"billIds": ids})
        if not isinstance(data, list) or len(data) != len(ids):
            raise EditError("账单数量不匹配，已停止整个批次。")
        records = {row.get("id"): row for row in data if isinstance(row, dict)}
        if len(records) != len(ids) or set(records) != set(ids):
            raise EditError("账单 ID 不匹配，已停止整个批次。")
        return records

    async def review_rows(self, updates: list[dict[str, str]],
                          records: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        account_changes = any("assetId" in item for item in updates)
        if account_changes and any("assetId" not in item for item in updates):
            raise EditError("账户迁移批次的每笔账单都必须明确目标账户。")
        books_data = await self.checked("list_books", {"includeHidden": True})
        assets_data = await self.checked("list_assets", {"includeHidden": True, "includeBalances": account_changes})
        if not isinstance(books_data, list) or not isinstance(assets_data, list):
            raise EditError("账本或账户目录无法核对。")
        if account_changes:
            info = await self.checked("get_user_info", {})
            if not isinstance(info, dict) or info.get("baseCurrency") != "CNY":
                raise EditError("账户迁移仅支持人民币本位币。")
        books = {str(b.get("bookId")): b for b in books_data if isinstance(b, dict)}
        assets = {str(a.get("id")): a for g in assets_data if isinstance(g, dict)
                  for a in g.get("children", []) if isinstance(a, dict)}
        by_book: dict[str, dict[str, dict[str, Any]]] = {}
        for book_id in {str(b.get("bookId")) for b in records.values()}:
            tree = await self.checked("list_categories", {"bookId": book_id})
            if not isinstance(tree, list):
                raise EditError("分类目录无法核对。")
            by_book[book_id] = {}
            for group in tree:
                if not isinstance(group, dict):
                    raise EditError("分类目录格式异常。")
                by_book[book_id][str(group.get("id"))] = {**group, "displayName": group.get("name", "")}
                for item in group.get("children", []):
                    if isinstance(item, dict):
                        by_book[book_id][str(item.get("id"))] = {
                            **item, "displayName": f"{group.get('name', '')} → {item.get('name', '')}"}
        rows = []
        account_pair: tuple[str, str, str] | None = None
        total_moved = Decimal("0.00")
        for update in updates:
            bill = records[update["billId"]]
            book_id, asset_id = str(bill.get("bookId")), str(bill.get("assetId"))
            category_id, target_id = str(bill.get("categoryId")), update["categoryId"]
            kind = bill.get("type")
            if kind not in (0, 1):
                raise EditError("批量分类仅支持普通收入或支出，已停止整个批次。")
            book, asset = books.get(book_id), assets.get(asset_id)
            cats = by_book.get(book_id, {})
            old, new = cats.get(category_id), cats.get(target_id)
            conversion = bill.get("currencyConversion")
            currency = conversion.get("sourceCurrency") if isinstance(conversion, dict) else None
            currency = currency or (asset.get("currency") if asset else None)
            if not all([book, asset, old, new, currency]) or new.get("type") != kind:
                raise EditError("账本、账户、币种或分类无法核对，或目标分类类型不匹配。")
            new_asset_id = update.get("assetId")
            if category_id == target_id and new_asset_id is None:
                raise EditError("至少一笔账单已是目标分类；请重新选择整批。")
            amount = conversion.get("sourceAmount", bill["money"]) if isinstance(conversion, dict) else bill["money"]
            row = {"billId": update["billId"], "bookId": book_id, "bookName": book["name"],
                   "assetId": asset_id, "assetName": asset["name"], "currency": currency,
                   "time": bill["time"], "type": kind, "money": amount,
                   "remark": bill.get("remark", ""), "oldCategoryId": category_id,
                   "oldCategoryName": old["displayName"], "newCategoryId": target_id,
                   "newCategoryName": new["displayName"]}
            if new_asset_id is not None:
                target_asset = assets.get(new_asset_id)
                pair = (book_id, asset_id, new_asset_id)
                if account_pair is not None and pair != account_pair:
                    raise EditError("账户迁移须在同一账本及同一对账户之间进行。")
                account_pair = pair
                if (kind != 0 or new_asset_id == asset_id or not target_asset
                    or asset.get("currency") != "CNY" or target_asset.get("currency") != "CNY"
                    or asset.get("inCount") != 1 or target_asset.get("inCount") != 1
                    or asset.get("status") != 0
                    or target_asset.get("status") != 0 or bill.get("discount") != 0
                    or conversion not in (None, {})):
                    raise EditError("账户迁移仅支持无优惠或换算的人民币普通支出，且两账户须计入资产并可用。")
                amount_to_move = cents(bill.get("money"))
                if amount_to_move <= 0:
                    raise EditError("待迁移金额必须大于零。")
                total_moved += amount_to_move
                row.update({"newAssetId": new_asset_id, "newAssetName": target_asset["name"]})
            jsonschema.Draft202012Validator(REVIEW_ITEM).validate(row)
            rows.append(row)
        balances = []
        if account_pair is not None:
            _, old_id, new_id = account_pair
            old_asset, new_asset = assets[old_id], assets[new_id]
            old_before, new_before = cents(old_asset.get("money")), cents(new_asset.get("money"))
            if new_before < total_moved:
                raise EditError("目标账户余额不足，不能进行本批迁移。")
            for item, delta, before in [(old_asset, total_moved, old_before), (new_asset, -total_moved, new_before)]:
                balances.append({"assetId": item["id"], "assetName": item["name"], "currency": "CNY",
                                 "before": str(before), "delta": str(delta), "after": str(before + delta)})
        return rows, balances

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if name not in BATCH_TOOL_NAMES:
                raise EditError("未知的批量操作。")
            jsonschema.Draft202012Validator(PREVIEW_SCHEMA if name == "preview_batch_categories" else APPLY_SCHEMA).validate(arguments)
            async with asyncio.timeout(105):
                if name == "preview_batch_categories":
                    return await self.preview(arguments["updates"])
                return await self.apply(arguments["planId"], arguments["reviewed"], arguments.get("balancePreview", []))
        except (jsonschema.ValidationError, EditError) as exc:
            message = str(exc) if isinstance(exc, EditError) else "批量操作参数无效。"
            return result({"saved": False, "message": message}, True)
        except Exception:
            if name == "apply_batch_categories":
                return result({"saved": None, "message": "批量提交结果不确定，可能部分生效；请核对每笔账单，不要自动重试。"}, True)
            return result({"saved": False, "message": "批量预览失败，未提交修改。"}, True)

    async def preview(self, updates: list[dict[str, str]]) -> types.CallToolResult:
        updates = copy.deepcopy(updates)
        ids = [item["billId"] for item in updates]
        if len(ids) != len(set(ids)):
            raise EditError("同一批次不能包含重复账单。")
        records = await self.read_records(updates)
        reviewed, balances = await self.review_rows(updates, records)
        upstream_updates = [{"billId": u["billId"], "patch": {
            "categoryId": u["categoryId"], **({"assetId": u["assetId"]} if "assetId" in u else {})}} for u in updates]
        jsonschema.validate({"updates": upstream_updates}, await self.schema("update_bills"))
        self.plans = {key: plan for key, plan in self.plans.items() if plan.expires > self.clock()}
        if len(self.plans) >= 20:
            raise EditError("待确认的批量预览过多，请等待过期后重试。")
        plan_id = secrets.token_hex(32)
        self.plans[plan_id] = BatchPlan(copy.deepcopy(reviewed), copy.deepcopy(records), copy.deepcopy(balances), self.clock() + self.ttl)
        return result({"saved": False, "planId": plan_id, "reviewed": reviewed, "balancePreview": balances,
                       "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat(),
                       "message": "仅基于当前缓存预览，尚未修改；提交前会刷新并核对全部记录和目录。请展示并确认完整批次。"})

    async def apply(self, plan_id: str, reviewed: list[dict[str, Any]],
                    balance_preview: list[dict[str, Any]]) -> types.CallToolResult:
        async with self.lock:
            plan = self.plans.pop(plan_id, None)
            if plan is None or plan.expires <= self.clock():
                raise EditError("预览不存在、已过期或已使用；不能重复提交。")
            if reviewed != plan.reviewed or balance_preview != plan.balance_preview:
                raise EditError("本次审批内容与预览不一致，未提交修改。")
            updates = [{"billId": row["billId"], "categoryId": row["newCategoryId"],
                        **({"assetId": row["newAssetId"]} if "newAssetId" in row else {})}
                       for row in plan.reviewed]
            await self.checked("refresh_cache", {})
            current = await self.read_records(updates)
            if any(fingerprint(current[key]) != fingerprint(plan.before[key]) for key in current):
                raise EditError("至少一笔账单已变化，未提交整个批次。")
            refreshed_rows, refreshed_balances = await self.review_rows(updates, current)
            if refreshed_rows != plan.reviewed or refreshed_balances != plan.balance_preview:
                raise EditError("账本、账户、余额或分类目录已变化，未提交整个批次。")
            upstream_updates = [{"billId": row["billId"], "patch": {"categoryId": row["newCategoryId"],
                **({"assetId": row["newAssetId"]} if "newAssetId" in row else {})}} for row in plan.reviewed]
            audit_entry = {"planId": plan_id, "reviewed": plan.reviewed,
                           "balancePreview": plan.balance_preview,
                           "time": datetime.now(timezone.utc).isoformat()}
            try:
                self.audit.append({**audit_entry, "status": "submit_started"})
            except OSError:
                raise EditError("无法保存批次核对记录，未提交修改。") from None
            try:
                response = await self.invoke("update_bills", {"updates": upstream_updates})
                data = self.payload(response)
                if not isinstance(data, list) or len(data) != len(upstream_updates):
                    raise ValueError("unexpected result length")
                after = {record.get("id"): record for record in data if isinstance(record, dict)}
                if set(after) != set(current):
                    raise ValueError("unexpected result ids")
                protected = ("bookId", "time", "type", "money", "remark", "tagIds",
                             "excludeFromIncomeExpense", "excludeFromBudget", "discount", "currencyConversion")
                for row in plan.reviewed:
                    bill_id = row["billId"]
                    if after[bill_id].get("categoryId") != row["newCategoryId"]:
                        raise ValueError("target category not confirmed")
                    if after[bill_id].get("assetId") != row.get("newAssetId", row["assetId"]):
                        raise ValueError("target account not confirmed")
                    if any(after[bill_id].get(key) != current[bill_id].get(key) for key in protected):
                        raise ValueError("another field changed")
                if plan.balance_preview:
                    await self.checked("refresh_cache", {})
                    groups = await self.checked("list_assets", {"includeHidden": True, "includeBalances": True})
                    assets = {str(a.get("id")): a for g in groups if isinstance(g, dict)
                              for a in g.get("children", []) if isinstance(a, dict)}
                    if any(row["assetId"] not in assets or
                           str(cents(assets[row["assetId"]].get("money"))) != row["after"]
                           for row in plan.balance_preview):
                        raise ValueError("post-write account balance not confirmed")
                self.audit.append({**audit_entry, "status": "confirmed", "records": data})
                return result({"saved": True, "records": data, "reviewed": plan.reviewed,
                               "balancePreview": plan.balance_preview,
                               "message": "上游返回整批同步确认的记录，请核对。"})
            except Exception:
                try:
                    self.audit.append({**audit_entry, "status": "unknown"})
                except OSError:
                    pass
                return result({"saved": None, "reviewed": plan.reviewed, "balancePreview": plan.balance_preview,
                               "message": "批量结果不确定，可能部分生效；逐笔核对后再决定下一步，不要自动重试。"}, True)
