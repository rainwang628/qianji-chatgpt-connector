"""Batch operations use only fabricated records and a fake upstream."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from mcp import types

from batch_category_edits import BatchCategoryEdits, PrivateAudit
from bill_edits import result


class FakeUpstream:
    def __init__(self):
        self.bills = {str(i): {"id": str(i), "bookId": "-1", "assetId": "700", "type": 0,
                               "time": "2026-01-02T10:00:00+08:00", "money": i * 2,
                               "remark": f"虚构账单 {i}", "categoryId": "200", "tagIds": [],
                               "excludeFromIncomeExpense": False, "excludeFromBudget": False, "discount": 0}
                      for i in (101, 102)}
        self.books = [{"bookId": "-1", "name": "虚构账本"}]
        self.assets = [{"name": "虚构分组", "children": [
            {"id": "700", "name": "虚构原账户", "currency": "CNY", "status": 0, "inCount": 1, "money": 1000},
            {"id": "701", "name": "虚构目标账户", "currency": "CNY", "status": 0, "inCount": 1, "money": 1000}]}]
        self.base_currency = "CNY"
        self.categories = [{"id": "200", "name": "虚构旧分类", "type": 0, "children": [
            {"id": "201", "name": "虚构新分类", "type": 0}]}]
        self.writes = []
        self.calls = []
        self.mode = "normal"

    async def schema(self, name):
        assert name == "update_bills"
        return {"type": "object", "required": ["updates"], "properties": {"updates": {"type": "array"}}}

    async def invoke(self, name, args):
        self.calls.append(name)
        if name == "refresh_cache":
            return result({"refreshed": True})
        if name == "get_bills":
            return result({"result": [copy.deepcopy(self.bills[key]) for key in args["billIds"] if key in self.bills]})
        if name == "list_books":
            return result({"result": copy.deepcopy(self.books)})
        if name == "list_assets":
            return result({"result": copy.deepcopy(self.assets)})
        if name == "list_categories":
            return result({"result": copy.deepcopy(self.categories)})
        if name == "get_user_info":
            return result({"baseCurrency": self.base_currency})
        assert name == "update_bills"
        self.writes.append(copy.deepcopy(args))
        for i, update in enumerate(args["updates"]):
            if self.mode == "partial" and i:
                break
            old_asset = self.bills[update["billId"]]["assetId"]
            new_asset = update["patch"].get("assetId", old_asset)
            if old_asset != new_asset:
                for asset in self.assets[0]["children"]:
                    if asset["id"] == old_asset:
                        asset["money"] += self.bills[update["billId"]]["money"]
                    if asset["id"] == new_asset:
                        asset["money"] -= self.bills[update["billId"]]["money"]
            self.bills[update["billId"]].update(update["patch"])
        if self.mode == "lost":
            raise TimeoutError("fictional lost response")
        if self.mode == "cancelled":
            raise asyncio.CancelledError()
        if self.mode == "partial":
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text="fictional partial")])
        if self.mode == "malformed":
            return result({"unexpected": True})
        return result({"result": [copy.deepcopy(self.bills[u["billId"]]) for u in args["updates"]]})


class BatchCategoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = FakeUpstream()
        self.now = 100.0
        self.edits = BatchCategoryEdits(self.upstream.invoke, self.upstream.schema, PrivateAudit(Path(self.tmp.name)),
                                        clock=lambda: self.now, ttl=300)
        self.updates = [{"billId": "101", "categoryId": "201"}, {"billId": "102", "categoryId": "201"}]

    async def preview(self):
        response = await self.edits.call("preview_batch_categories", {"updates": copy.deepcopy(self.updates)})
        self.assertFalse(response.isError, response.model_dump_json())
        return response.structuredContent

    async def apply(self, plan):
        return await self.edits.call("apply_batch_categories", {"planId": plan["planId"],
            "reviewed": plan["reviewed"], "balancePreview": plan.get("balancePreview", [])})

    def audit_entries(self):
        path = Path(self.tmp.name) / "batch-category-audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def test_preview_shows_every_record_without_writing(self):
        plan = await self.preview()
        self.assertFalse(plan["saved"])
        self.assertEqual([row["billId"] for row in plan["reviewed"]], ["101", "102"])
        self.assertEqual(plan["reviewed"][0]["newCategoryName"], "虚构旧分类 → 虚构新分类")
        self.assertEqual(plan["reviewed"][0]["currency"], "CNY")
        self.assertEqual(self.upstream.writes, [])
        self.assertNotIn("refresh_cache", self.upstream.calls)

    async def test_batch_submits_once_records_audit_and_refuses_replay(self):
        plan = await self.preview()
        saved = await self.apply(plan)
        self.assertTrue(saved.structuredContent["saved"], saved.model_dump_json())
        self.assertEqual(len(self.upstream.writes), 1)
        self.assertEqual(len(self.upstream.writes[0]["updates"]), 2)
        self.assertEqual([entry["status"] for entry in self.audit_entries()], ["submit_started", "confirmed"])
        self.assertEqual((Path(self.tmp.name) / "batch-category-audit.jsonl").stat().st_mode & 0o777, 0o600)
        replay = await self.apply(plan)
        self.assertTrue(replay.isError)
        self.assertEqual(len(self.upstream.writes), 1)

    async def test_concurrent_submission_only_writes_once(self):
        plan = await self.preview()
        responses = await asyncio.gather(self.apply(plan), self.apply(plan))
        self.assertEqual(sum(not r.isError for r in responses), 1)
        self.assertEqual(len(self.upstream.writes), 1)

    async def test_edited_review_is_rejected(self):
        plan = await self.preview()
        plan["reviewed"][0]["newCategoryId"] = "202"
        response = await self.apply(plan)
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_changed_record_rejects_entire_batch(self):
        plan = await self.preview()
        self.upstream.bills["102"]["money"] = 88
        response = await self.apply(plan)
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])
        self.assertEqual(self.audit_entries(), [])

    async def test_renamed_category_after_preview_rejects_batch(self):
        plan = await self.preview()
        self.upstream.categories[0]["children"][0]["name"] = "不同名称"
        response = await self.apply(plan)
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_expired_and_forged_plans_fail(self):
        plan = await self.preview()
        self.now += 301
        self.assertTrue((await self.apply(plan)).isError)
        plan["planId"] = "a" * 64
        self.assertTrue((await self.apply(plan)).isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_duplicate_ids_extra_fields_and_large_batches_rejected(self):
        for updates in [self.updates + [self.updates[0]],
                        [{**self.updates[0], "money": 999}],
                        self.updates * 6, []]:
            response = await self.edits.call("preview_batch_categories", {"updates": updates})
            self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_invalid_target_noop_and_unsupported_type_are_rejected(self):
        for update in [[{"billId": "101", "categoryId": "999"}],
                       [{"billId": "101", "categoryId": "200"}]]:
            self.assertTrue((await self.edits.call("preview_batch_categories", {"updates": update})).isError)
        self.upstream.bills["102"]["type"] = 2
        self.assertTrue((await self.edits.call("preview_batch_categories", {"updates": self.updates})).isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_missing_bill_and_missing_asset_are_rejected(self):
        self.assertTrue((await self.edits.call("preview_batch_categories", {"updates": [
            {"billId": "999", "categoryId": "201"}]})).isError)
        self.upstream.bills["101"]["assetId"] = None
        self.assertTrue((await self.edits.call("preview_batch_categories", {"updates": self.updates})).isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_cross_currency_preview_uses_source_amount_and_currency(self):
        self.upstream.bills["101"]["currencyConversion"] = {"sourceCurrency": "USD", "sourceAmount": 15.25}
        plan = await self.preview()
        self.assertEqual(plan["reviewed"][0]["currency"], "USD")
        self.assertEqual(plan["reviewed"][0]["money"], 15.25)

    async def preview_move(self):
        updates = [{**item, "assetId": "701"} for item in self.updates]
        response = await self.edits.call("preview_batch_categories", {"updates": updates})
        self.assertFalse(response.isError, response.model_dump_json())
        return response.structuredContent

    async def test_batch_move_previews_and_checks_aggregate_balances(self):
        plan = await self.preview_move()
        self.assertEqual([r["newAssetId"] for r in plan["reviewed"]], ["701", "701"])
        self.assertEqual([(r["before"], r["delta"], r["after"]) for r in plan["balancePreview"]],
                         [("1000.00", "406.00", "1406.00"), ("1000.00", "-406.00", "594.00")])
        saved = await self.apply(plan)
        self.assertFalse(saved.isError, saved.model_dump_json())
        self.assertTrue(saved.structuredContent["saved"])
        self.assertEqual(len(self.upstream.writes), 1)
        self.assertEqual(self.upstream.writes[0]["updates"][0]["patch"], {"categoryId": "201", "assetId": "701"})
        self.assertEqual([self.upstream.bills[i]["assetId"] for i in ("101", "102")], ["701", "701"])

    async def test_move_allows_one_bill_with_category_already_correct(self):
        self.upstream.bills["102"]["categoryId"] = "201"
        plan = await self.preview_move()
        self.assertEqual(plan["reviewed"][1]["oldCategoryId"], "201")
        self.assertEqual(plan["reviewed"][1]["newCategoryId"], "201")
        response = await self.apply(plan)
        self.assertFalse(response.isError, response.model_dump_json())
        self.assertEqual(self.upstream.bills["102"]["assetId"], "701")

    async def test_balance_change_after_preview_rejects_entire_batch(self):
        plan = await self.preview_move()
        self.upstream.assets[0]["children"][1]["money"] += 1
        response = await self.apply(plan)
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_tampering_balance_preview_prevents_write(self):
        plan = await self.preview_move()
        plan["balancePreview"][0]["after"] = "9999.00"
        self.assertTrue((await self.apply(plan)).isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_move_rejects_unsupported_account_and_bill_conditions(self):
        mutations = [
            lambda: self.upstream.assets[0]["children"][1].update(currency="USD"),
            lambda: self.upstream.assets[0]["children"][1].update(status=2),
            lambda: self.upstream.assets[0]["children"][1].update(inCount=0),
            lambda: self.upstream.assets[0]["children"][1].update(money=400),
            lambda: self.upstream.bills["101"].update(discount=1),
            lambda: self.upstream.bills["101"].update(currencyConversion={"sourceCurrency": "USD"}),
            lambda: self.upstream.bills["101"].update(type=1),
            lambda: setattr(self.upstream, "base_currency", "USD"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.upstream = FakeUpstream()
                self.edits = BatchCategoryEdits(self.upstream.invoke, self.upstream.schema, PrivateAudit(Path(self.tmp.name)))
                mutate()
                updates = [{**item, "assetId": "701"} for item in self.updates]
                self.assertTrue((await self.edits.call("preview_batch_categories", {"updates": updates})).isError)
                self.assertEqual(self.upstream.writes, [])

    async def test_mixed_account_moves_and_category_only_are_rejected(self):
        updates = [{**self.updates[0], "assetId": "701"}, self.updates[1]]
        self.assertTrue((await self.edits.call("preview_batch_categories", {"updates": updates})).isError)

    async def test_partial_account_move_is_unknown_and_cannot_be_replayed(self):
        plan = await self.preview_move()
        self.upstream.mode = "partial"
        response = await self.apply(plan)
        self.assertTrue(response.isError)
        self.assertIsNone(response.structuredContent["saved"])
        self.assertTrue((await self.apply(plan)).isError)
        self.assertEqual(len(self.upstream.writes), 1)

    async def test_cancellation_after_send_leaves_audit_and_blocks_replay(self):
        plan = await self.preview()
        self.upstream.mode = "cancelled"
        with self.assertRaises(asyncio.CancelledError):
            await self.apply(plan)
        self.assertEqual([entry["status"] for entry in self.audit_entries()], ["submit_started"])
        self.assertTrue((await self.apply(plan)).isError)
        self.assertEqual(len(self.upstream.writes), 1)

    async def test_failure_after_send_is_unknown_and_cannot_be_replayed(self):
        for mode in ("partial", "lost", "malformed"):
            with self.subTest(mode=mode):
                upstream = FakeUpstream()
                edits = BatchCategoryEdits(upstream.invoke, upstream.schema, PrivateAudit(Path(self.tmp.name)))
                plan = (await edits.call("preview_batch_categories", {"updates": self.updates})).structuredContent
                upstream.mode = mode
                args = {"planId": plan["planId"], "reviewed": plan["reviewed"]}
                response = await edits.call("apply_batch_categories", args)
                self.assertTrue(response.isError)
                self.assertIsNone(response.structuredContent["saved"])
                self.assertTrue((await edits.call("apply_batch_categories", args)).isError)
                self.assertEqual(len(upstream.writes), 1)

    async def test_audit_failure_prevents_any_write(self):
        plan = await self.preview()
        self.edits.audit = PrivateAudit(Path(self.tmp.name) / "missing")
        response = await self.apply(plan)
        self.assertTrue(response.isError)
        self.assertFalse(response.structuredContent["saved"])
        self.assertEqual(self.upstream.writes, [])

    def test_annotations_mark_writes_and_previews_correctly(self):
        tools = {tool.name: tool for tool in self.edits.tools()}
        self.assertTrue(tools["preview_batch_categories"].annotations.readOnlyHint)
        self.assertFalse(tools["apply_batch_categories"].annotations.readOnlyHint)
        self.assertTrue(tools["apply_batch_categories"].annotations.destructiveHint)
        self.assertFalse(tools["apply_batch_categories"].annotations.idempotentHint)
