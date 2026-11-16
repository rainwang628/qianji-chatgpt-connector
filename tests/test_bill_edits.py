"""Edits are exercised only against fictional records and an in-memory upstream."""

import asyncio
import copy
import unittest

from bill_edits import BillEdits, PATCH_FIELDS, result


class FakeUpstream:
    def __init__(self):
        self.bills = {"42": {"id": "42", "bookId": "-1", "type": 0, "money": 35,
                              "remark": "虚构午餐", "updateTime": "2026-11-11T10:00:00+08:00"}}
        self.writes = []
        self.mode = "normal"

    async def schema(self, tool):
        patch = {"type": "object", "additionalProperties": False,
                 "properties": {k: v for k, v in PATCH_FIELDS.items() if k != "fee" or tool == "update_transfer"}}
        fields = {"billId": {"type": "string"}, "patch": patch}
        if tool == "update_bills":
            return {"type": "object", "properties": {"updates": {"type": "array", "items": {
                "type": "object", "properties": fields, "required": ["billId", "patch"]}}}, "required": ["updates"]}
        return {"type": "object", "properties": fields if tool == "update_transfer" else {
            "refundBillId": {"type": "string"}, "patch": patch}}

    async def invoke(self, name, args):
        if name == "refresh_cache":
            return result({"refreshed": True})
        if name == "get_bills":
            return result({"result": [copy.deepcopy(self.bills[key]) for key in args["billIds"] if key in self.bills]})
        self.writes.append((name, copy.deepcopy(args)))
        if name == "update_bills":
            change = args["updates"][0]
            key, patch = change["billId"], change["patch"]
        else:
            key, patch = args.get("billId", args.get("refundBillId")), args["patch"]
        self.bills[key].update(patch)
        self.bills[key]["updateTime"] = "2026-11-11T10:01:00+08:00"
        if self.mode == "transport_after_write":
            raise TimeoutError("fictional response lost after update")
        if self.mode == "error_after_write":
            from mcp import types
            return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text="fictional sync failure")])
        if self.mode == "malformed_success":
            return result({"unexpected": True})
        if self.mode == "unparseable_success":
            from mcp import types
            return types.CallToolResult(content=[types.TextContent(type="text", text="not JSON")])
        return result({"result": [copy.deepcopy(self.bills[key])]})


class BillEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.now = 100.0
        self.edits = BillEdits(self.upstream.invoke, self.upstream.schema, clock=lambda: self.now, ttl=300)

    async def preview(self, patch=None):
        response = await self.edits.call("preview_bill_edit", {"billId": "42", "patch": patch or {"remark": "虚构新备注"}})
        self.assertFalse(response.isError, response.model_dump_json())
        return response.structuredContent

    async def test_preview_reads_but_never_writes(self):
        data = await self.preview()
        self.assertFalse(data["saved"])
        self.assertEqual(data["changes"], [{"field": "remark", "before": "虚构午餐", "after": "虚构新备注"}])
        self.assertEqual(self.upstream.writes, [])
        self.assertEqual(self.upstream.bills["42"]["remark"], "虚构午餐")

    async def test_saved_plan_is_immutable_and_can_be_used_once(self):
        patch = {"remark": "虚构批准备注"}
        plan = await self.preview(patch)
        patch["remark"] = "caller tried changing an already previewed patch"
        saved = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
        self.assertTrue(saved.structuredContent["saved"])
        self.assertEqual(self.upstream.bills["42"]["remark"], "虚构批准备注")
        replay = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
        self.assertTrue(replay.isError)
        self.assertEqual(len(self.upstream.writes), 1)

    async def test_concurrent_submit_writes_once(self):
        plan = await self.preview()
        responses = await asyncio.gather(*(self.edits.call("apply_bill_edit", {"planId": plan["planId"]}) for _ in range(2)))
        self.assertEqual(sum(not r.isError for r in responses), 1)
        self.assertEqual(len(self.upstream.writes), 1)

    async def test_changed_record_rejected_without_overwriting(self):
        plan = await self.preview()
        self.upstream.bills["42"]["money"] = 99
        response = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
        self.assertTrue(response.isError)
        self.assertIn("已变化", response.structuredContent["message"])
        self.assertEqual(self.upstream.writes, [])

    async def test_expired_plan_rejected(self):
        plan = await self.preview()
        self.now += 301
        response = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_forged_plan_or_extra_override_rejected(self):
        for args in [{"planId": "a" * 64}, {"planId": "a" * 64, "patch": {"money": 1}}]:
            response = await self.edits.call("apply_bill_edit", args)
            self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_invalid_parameters_and_type_changes_rejected(self):
        for patch in [{}, {"money": 0}, {"money": -1}, {"money": 1.001}, {"money": float("nan")},
                      {"remark": "x" * 501}, {"type": 1}, {"billId": "100"}, {"time": "tomorrow"},
                      {"time": "2026-11-11T10:00:00"}, {"time": "2026-02-30T10:00:00+08:00"},
                      {"tagIds": ["same", "same"]}]:
            response = await self.edits.call("preview_bill_edit", {"billId": "42", "patch": patch})
            self.assertTrue(response.isError, patch)
        self.assertEqual(self.upstream.writes, [])

    async def test_valid_time_and_amount_can_be_previewed(self):
        data = await self.preview({"time": "2026-11-11T11:00:00+08:00", "money": 12.34})
        self.assertEqual(len(data["changes"]), 2)
        self.assertEqual(self.upstream.writes, [])

    async def test_transfer_adjustments_include_implicit_clear_and_enforce_limits(self):
        self.upstream.bills["42"].update(type=2, fee=0, discount=3)
        data = await self.preview({"fee": 1})
        self.assertIn({"field": "discount", "before": 3, "after": 0}, data["changes"])
        saved_patch = self.edits.plans[data["planId"]].arguments["patch"]
        self.assertEqual(saved_patch, {"fee": 1, "discount": 0})
        for patch in [{"fee": 1, "discount": 2}, {"money": 2}, {"discount": 36}]:
            response = await self.edits.call("preview_bill_edit", {"billId": "42", "patch": patch})
            self.assertTrue(response.isError, patch)
        self.assertEqual(self.upstream.writes, [])

    async def test_unsupported_bill_scenario_rejected(self):
        self.upstream.bills["42"]["type"] = 6
        response = await self.edits.call("preview_bill_edit", {"billId": "42", "patch": {"remark": "test"}})
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_missing_bill_rejected(self):
        response = await self.edits.call("preview_bill_edit", {"billId": "999", "patch": {"remark": "test"}})
        self.assertTrue(response.isError)
        self.assertEqual(self.upstream.writes, [])

    async def test_scenario_specific_schema_is_checked(self):
        response = await self.edits.call("preview_bill_edit", {"billId": "42", "patch": {"fee": 1}})
        self.assertTrue(response.isError)
        self.assertEqual(self.edits.plans, {})

    async def test_noop_does_not_issue_write_plan(self):
        data = await self.preview({"remark": "虚构午餐"})
        self.assertNotIn("planId", data)
        self.assertEqual(self.upstream.writes, [])

    async def test_each_supported_type_uses_its_own_update_endpoint(self):
        for kind, endpoint in [(0, "update_bills"), (1, "update_bills"), (5, "update_bills"),
                               (2, "update_transfer"), (3, "update_transfer"), (20, "update_refund")]:
            self.upstream.bills["42"]["type"] = kind
            plan = await self.preview({"remark": f"虚构编辑 {kind}"})
            saved = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
            self.assertTrue(saved.structuredContent["saved"])
            self.assertEqual(self.upstream.writes[-1][0], endpoint)

    async def test_unknown_write_outcome_blocks_replay_without_claiming_no_write(self):
        for mode in ["transport_after_write", "error_after_write", "malformed_success", "unparseable_success"]:
            self.setUp()
            plan = await self.preview()
            self.upstream.mode = mode
            saved = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
            self.assertTrue(saved.isError)
            self.assertIsNone(saved.structuredContent["saved"])
            replay = await self.edits.call("apply_bill_edit", {"planId": plan["planId"]})
            self.assertTrue(replay.isError)
            self.assertEqual(len(self.upstream.writes), 1)
            self.assertEqual(self.upstream.bills["42"]["remark"], "虚构新备注")

    def test_apply_is_labeled_as_destructive_write(self):
        tools = {tool.name: tool for tool in self.edits.tools()}
        self.assertTrue(tools["preview_bill_edit"].annotations.readOnlyHint)
        self.assertFalse(tools["apply_bill_edit"].annotations.readOnlyHint)
        self.assertTrue(tools["apply_bill_edit"].annotations.destructiveHint)
        self.assertFalse(tools["apply_bill_edit"].annotations.idempotentHint)
