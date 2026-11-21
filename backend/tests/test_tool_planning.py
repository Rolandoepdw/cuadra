"""L3.6: the model proposes read tools; the policy validates and falls back."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from cuadra.ai import llm
from cuadra.ai.tools import base
from cuadra.ai.tools import catalog as catalog_tools
from cuadra.ai.tools import escalations as escalations_tools
from cuadra.ai.tools import memory as memory_tools
from cuadra.ai.tools import sales as sales_tools


def _registry() -> base.Registry:
    registry = base.Registry()
    catalog_tools.register(registry)
    sales_tools.register(registry)
    memory_tools.register(registry)
    escalations_tools.register(registry)
    return registry


class PlanToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_valid_plan_is_parsed(self):
        registry = _registry()
        raw = json.dumps(
            {"calls": [{"name": "catalog.search", "args": {"query": "laptops"}}]}
        )
        with patch("cuadra.ai.llm.chat", new=_async_return(raw)):
            calls = await llm.plan_tools(
                [{"role": "user", "content": "¿qué laptops tienes?"}],
                registry.allowed("on"),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "catalog.search")
        self.assertEqual(calls[0].args["query"], "laptops")

    async def test_unknown_tool_and_invalid_args_are_dropped(self):
        registry = _registry()
        raw = json.dumps(
            {
                "calls": [
                    {"name": "catalog.search", "args": {"query": "laptops"}},
                    {"name": "catalog.inventada", "args": {}},
                    {"name": "zones.list", "args": {"product_id": 0}},
                ]
            }
        )
        with patch("cuadra.ai.llm.chat", new=_async_return(raw)):
            calls = await llm.plan_tools(
                [{"role": "user", "content": "hola"}], registry.allowed("on")
            )
        self.assertEqual([call.name for call in calls], ["catalog.search"])

    async def test_write_tools_are_not_exposed(self):
        registry = base.Registry()
        read = base.ToolSpec(
            name="catalog.search",
            domain="catalog",
            owner="catalog",
            description="lee",
            permission=base.READ,
        )
        write = base.ToolSpec(
            name="catalog.write",
            domain="catalog",
            owner="catalog",
            description="escribe",
            permission=base.WRITE,
        )
        registry.register(read, lambda conn: {})
        registry.register(write, lambda conn: {})
        captured = {}

        async def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return json.dumps({"calls": []})

        with patch("cuadra.ai.llm.chat", new=fake_chat):
            await llm.plan_tools(
                [{"role": "user", "content": "hola"}], registry.allowed("on")
            )
        system = captured["messages"][0]["content"]
        self.assertIn("catalog.search", system)
        self.assertNotIn("catalog.write", system)

    async def test_the_prompt_has_no_sql(self):
        registry = _registry()
        captured = {}

        async def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return json.dumps({"calls": []})

        with patch("cuadra.ai.llm.chat", new=fake_chat):
            await llm.plan_tools(
                [{"role": "user", "content": "hola"}], registry.allowed("on")
            )
        system = captured["messages"][0]["content"].lower()
        self.assertNotIn("select ", system)
        self.assertNotIn("create table", system)


class GoldenToolsTests(unittest.TestCase):
    def test_golden_tools_exist_and_args_validate(self):
        path = Path(__file__).resolve().parent / "golden" / "tools.jsonl"
        registry = _registry()
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(rows)
        for row in rows:
            spec = registry.get(row["expected_tool"])
            self.assertEqual(spec.permission, base.READ)
            if spec.input_model is not None:
                spec.input_model.model_validate(row["expected_args"])
            for name in row.get("must_not_call") or []:
                self.assertTrue(registry.has(name))
                self.assertNotEqual(name, row["expected_tool"])


def _async_return(value):
    async def inner(*args, **kwargs):
        return value

    return inner


if __name__ == "__main__":
    unittest.main()
