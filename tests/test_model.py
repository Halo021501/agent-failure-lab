"""Deterministic provider simulation. These tests make zero external model requests."""
import copy
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lab.common import ROOT, canonical, Halt, LabError
from lab.compare import compare, summary
from lab.model_config import ModelConfig, load_model_config, save_model_config
from lab.model_protocol import TOOLS, instructions
from lab.replay import normalized_events
from lab.runner import Runner
from lab.schemas import Budget
from lab.storage import Store


def response(calls=(), usage=True, status="completed", extra=()):
    value = {"id": "resp-mock", "model": "mock-model", "status": status, "output": list(extra) + [
        {"type": "function_call", "name": tool, "arguments": args if isinstance(args, str) else canonical(args), "call_id": "provider-" + str(index), "status": "completed"}
        for index, (tool, args) in enumerate(calls)]}
    if usage:
        value["usage"] = {"input_tokens": 10, "output_tokens": 5}
    return value


class FakeTransport:
    def __init__(self, responses, count=10, action=None):
        self.responses, self.counted, self.action = [item if isinstance(item, Exception) else copy.deepcopy(item) for item in responses], count, action
        self.requests, self.count_requests = [], []

    def count(self, payload, cancel, deadline):
        self.count_requests.append(copy.deepcopy(payload))
        return {"input_tokens": self.counted}

    def respond(self, payload, cancel, deadline):
        self.requests.append(copy.deepcopy(payload))
        if self.action:
            self.action(cancel)
        if not self.responses:
            raise AssertionError("Unexpected extra provider request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ModelTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / "data/test-tmp"
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=directory)
        self.store = Store(Path(self.temp.name))
        self.env = patch.dict(os.environ, {"LAB_TEST_CREDENTIAL": "fixture-credential-never-logged"})
        self.env.start()
        self.config = ModelConfig(model="mock-model", api_key_env="LAB_TEST_CREDENTIAL")

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def runner(self, transport, config=None):
        return Runner(self.store, model_config=config or self.config, transport_factory=lambda _: transport)

    def execute(self, transport, config=None, case="N01", budget=None):
        runner = self.runner(transport, config)
        eid = runner.create_experiment([case], "model-recovery", budget=budget, agent_kind="model")
        return runner, runner.execute_experiment(eid)["runs"][0]

    def recorded_public_calls(self, case="N01"):
        # The fixture is derived from public tool execution, never from expected answers.
        runner = Runner(self.store)
        eid = runner.create_experiment([case])
        source = runner.execute_experiment(eid)["runs"][0]
        calls = [(e["payload"]["tool"], e["payload"]["args"]) for e in self.store.events(source["id"]) if e["kind"] == "tool_call"]
        return source, calls

    def script_responses(self, calls, grouped=False):
        batches = [calls] if grouped else [[call] for call in calls]
        results = []
        for number, batch in enumerate(batches):
            value = response(batch)
            for index, item in enumerate(value["output"]):
                item["call_id"] = f"provider-{number}-{index}"
            results.append(value)
        results.append(response())
        return results

    def test_public_tool_loop_and_provider_usage_metadata(self):
        _, calls = self.recorded_public_calls()
        fake = FakeTransport(self.script_responses(calls))
        _, run = self.execute(fake)
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["evaluation"]["contract_pass"])
        self.assertEqual(run["metadata"]["model_calls"], len(calls) + 1)
        self.assertEqual(run["metadata"]["input_tokens"], 10 * (len(calls) + 1))
        self.assertEqual(run["metadata"]["output_tokens"], 5 * (len(calls) + 1))
        self.assertEqual(run["integrity"]["state"], "valid")
        for payload in fake.requests:
            text = canonical(payload)
            self.assertNotIn("fixture-credential", text)
            self.assertNotIn("expected_counts", text)
            self.assertNotIn("fault_ids", text)
            self.assertFalse(payload["store"])
            self.assertFalse(payload["parallel_tool_calls"])
        events = canonical(self.store.events(run["id"]))
        self.assertNotIn("fixture-credential", events)
        self.assertIsNone(summary([run])["cost"])

    def test_multiple_function_calls_are_sequential(self):
        _, calls = self.recorded_public_calls()
        fake = FakeTransport(self.script_responses(calls, grouped=True))
        _, run = self.execute(fake)
        self.assertTrue(run["evaluation"]["contract_pass"])
        outputs = [item for item in fake.requests[1]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), len(calls))
        self.assertEqual(run["metadata"]["model_calls"], 2)

    def test_invalid_arguments_and_unknown_tool_not_repaired(self):
        values = [response([("read_source", "{broken"), ("read_source", {"source_id": ["primary-release"]}), ("imagined_tool", {})]), response()]
        fake = FakeTransport(values)
        _, run = self.execute(fake)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["metadata"]["tool_calls"], 3)
        errors = [e["payload"]["code"] for e in self.store.events(run["id"]) if e["kind"] == "tool_error"]
        self.assertEqual(errors[:2], ["invalid_arguments", "invalid_arguments"])
        outputs = [json.loads(i["output"]) for i in fake.requests[1]["input"] if i.get("type") == "function_call_output"]
        self.assertTrue(all("error" in item for item in outputs))
        self.assertIsNone(self.store.optional_report(run["id"]))

    def test_missing_usage_stops_and_keeps_conservative_reservation(self):
        fake = FakeTransport([response(usage=False), response()])
        _, run = self.execute(fake)
        self.assertEqual(run["metadata"]["error_code"], "model_usage_unknown")
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(run["metadata"]["usage_unknown_calls"], 1)
        self.assertEqual(run["metadata"]["reserved_output_tokens"], self.config.max_output_tokens_per_response)
        self.assertIsNone(summary([run])["cost"])

    def test_input_preflight_prevents_generation(self):
        fake = FakeTransport([response()], count=101)
        _, run = self.execute(fake, replace(self.config, max_input_tokens=100))
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(len(fake.requests), 0)
        self.assertEqual(run["metadata"]["model_calls"], 0)

    def test_model_call_budget(self):
        first = response([("list_sources", {"scope": "primary"})])
        fake = FakeTransport([first, response()])
        _, run = self.execute(fake, budget=Budget(max_model_calls=1))
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(len(fake.requests), 1)

    def test_output_budget_accounts_and_stops_before_next_request(self):
        fake = FakeTransport([response([("list_sources", {"scope":"primary"})]), response()])
        config = replace(self.config, max_output_tokens=16, max_output_tokens_per_response=16)
        _, run = self.execute(fake, config)
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["metadata"]["output_tokens"], 5)
        self.assertEqual(len(fake.requests), 1)

    def test_provider_usage_above_limit_stops_before_tools(self):
        value = response([("list_sources", {"scope":"primary"})])
        value["usage"]["output_tokens"] = 17
        fake = FakeTransport([value])
        _, run = self.execute(fake, replace(self.config, max_output_tokens=32, max_output_tokens_per_response=16))
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["metadata"]["output_tokens"], 17)
        self.assertEqual(run["metadata"]["tool_calls"], 0)

    def test_invalid_preflight_does_not_generate(self):
        fake = FakeTransport([response()], count=True)
        _, run = self.execute(fake)
        self.assertEqual(run["metadata"]["error_code"], "model_protocol_error")
        self.assertEqual(len(fake.requests), 0)

    def test_model_partial_gap_uses_observed_content_without_script_parse_events(self):
        _, calls = self.recorded_public_calls("F-S04")
        fake = FakeTransport(self.script_responses(calls, grouped=True))
        _, run = self.execute(fake, case="F-S04")
        self.assertTrue(run["evaluation"] and run["evaluation"]["contract_pass"], run)
        self.assertFalse(run["evaluation"]["complete_success"])
        self.assertFalse(any(event["kind"] == "parse_error" for event in self.store.events(run["id"])))

    def test_recovered_invalid_read_does_not_break_partial_evaluator(self):
        _, calls = self.recorded_public_calls("F-S04")
        fake = FakeTransport(self.script_responses([("read_source", {"source_id":["primary-tests"]})] + calls, grouped=True))
        _, run = self.execute(fake, case="F-S04")
        self.assertTrue(run["evaluation"] and run["evaluation"]["contract_pass"], run)

    def test_network_budget_includes_token_count(self):
        fake = FakeTransport([response([("list_sources", {"scope": "primary"})]), response()])
        _, run = self.execute(fake, replace(self.config, max_network_requests=2))
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["metadata"]["network_requests"], 2)

    def test_tool_budget_applies_inside_multi_call(self):
        fake = FakeTransport([response([("list_sources", {"scope": "primary"}), ("list_sources", {"scope": "backup"})])])
        _, run = self.execute(fake, budget=Budget(max_tool_calls=1))
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["metadata"]["tool_calls"], 1)

    def test_batch_budget_persists_and_stops_later_cases(self):
        fake = FakeTransport([response(), response()])
        config = replace(self.config, max_batch_model_calls=1)
        runner = self.runner(fake, config)
        eid = runner.create_experiment(["N01", "N02"], "model-minimal", agent_kind="model")
        result = runner.execute_experiment(eid)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(sorted(r["status"] for r in result["runs"]), ["budget_exceeded", "failed"])
        self.assertEqual(self.store.db.execute("SELECT model_calls FROM model_budgets WHERE experiment_id=?", (eid,)).fetchone()[0], 1)
        self.assertFalse(summary(result["runs"])["experiment_complete"])
        self.assertEqual(summary(result["runs"])["model_execution_coverage"], .5)
        self.assertFalse(compare(result, result)["comparable"])

    def test_batch_token_reservation_is_atomic(self):
        runner = self.runner(FakeTransport([]))
        eid = runner.create_experiment(["N01", "N02"], "model-minimal", agent_kind="model")
        ids = [r["id"] for r in self.store.experiment_info(eid)["runs"]]
        config = replace(self.config, max_batch_input_tokens=10, max_batch_output_tokens=20)
        barrier, outcomes = threading.Barrier(2), []
        def reserve(run_id):
            barrier.wait()
            try:
                self.store.reserve_model_budget(run_id, 10, 20, config)
                outcomes.append("reserved")
            except Halt:
                outcomes.append("blocked")
        threads = [threading.Thread(target=reserve, args=(run_id,)) for run_id in ids]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(outcomes), ["blocked", "reserved"])

    def test_cancel_in_flight_keeps_unknown_usage_and_no_extra_request(self):
        def cancel_request(cancel):
            cancel.set()
            raise Halt("cancelled", "Cancelled fixture request")
        fake = FakeTransport([response()], action=cancel_request)
        runner, run = self.execute(fake)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["metadata"]["usage_unknown_calls"], 1)
        self.assertEqual(len(fake.requests), 1)
        self.assertNotIn(run["id"], runner.cancellations)

    def test_timeout_no_automatic_retry(self):
        fake = FakeTransport([LabError("model_timeout", "fixture timeout"), response()])
        _, run = self.execute(fake)
        self.assertEqual(run["metadata"]["error_code"], "model_timeout")
        self.assertEqual(run["metadata"]["usage_unknown_calls"], 1)
        self.assertEqual(len(fake.requests), 1)

    def test_incomplete_response_accounts_usage_without_executing_tools(self):
        fake = FakeTransport([response([("list_sources", {"scope": "primary"})], status="incomplete")])
        _, run = self.execute(fake)
        self.assertEqual(run["metadata"]["error_code"], "model_incomplete")
        self.assertEqual(run["metadata"]["output_tokens"], 5)
        self.assertEqual(run["metadata"]["tool_calls"], 0)

    def test_duplicate_provider_ids_stop(self):
        fake = FakeTransport([response([("list_sources", {"scope": "primary"})]), response([("list_sources", {"scope": "backup"})])])
        _, run = self.execute(fake)
        self.assertEqual(run["metadata"]["error_code"], "model_protocol_error")
        self.assertEqual(run["metadata"]["tool_calls"], 1)

    def test_opaque_reasoning_carried_in_memory_only(self):
        reasoning = {"type": "reasoning", "id": "rs-mock", "encrypted_content": "OPAQUE-TEST", "summary": []}
        fake = FakeTransport([response([("list_sources", {"scope": "primary"})], extra=[reasoning]), response()])
        _, run = self.execute(fake)
        self.assertIn(reasoning, fake.requests[1]["input"])
        self.assertNotIn("OPAQUE-TEST", canonical(self.store.events(run["id"])))
        self.assertNotIn('"type":"reasoning"', canonical(self.store.events(run["id"])))

    def test_lost_ack_retried_by_model_same_content_and_key(self):
        _, calls = self.recorded_public_calls("F-D04")
        fake = FakeTransport(self.script_responses(calls))
        _, run = self.execute(fake, case="F-D04")
        self.assertTrue(run["evaluation"]["contract_pass"])
        events = self.store.events(run["id"])
        self.assertEqual(sum(e["kind"] == "artifact_committed" for e in events), 1)
        self.assertTrue(any(e["kind"] == "tool_error" for e in events))
        no_retry = FakeTransport(self.script_responses(calls[:-1]))
        _, failed = self.execute(no_retry, case="F-D04")
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["evaluation"]["contract_pass"])
        self.assertEqual(failed["evaluation"]["facts_missing"], 0)

    def test_trace_zero_calls_and_tool_replay_new_model_usage(self):
        _, calls = self.recorded_public_calls()
        fake = FakeTransport(self.script_responses(calls) * 2)
        runner, run = self.execute(fake)
        count = len(fake.requests)
        _, trace_id = runner.replay(run["id"])
        self.assertEqual(len(fake.requests), count)
        self.assertEqual(self.store.run(trace_id)["metadata"]["executed_model_calls"], 0)
        self.assertEqual(normalized_events(self.store.events(trace_id)), normalized_events(self.store.events(run["id"])))
        _, tool_id = runner.replay(run["id"], "tool_replay")
        tool = self.store.run(tool_id)
        self.assertEqual(tool["status"], "completed")
        self.assertEqual(tool["metadata"]["executed_tool_calls"], 0)
        self.assertEqual(tool["metadata"]["executed_model_calls"], count)

    def test_model_conditions_disable_comparison(self):
        _, calls = self.recorded_public_calls()
        fake = FakeTransport(self.script_responses(calls) * 2)
        runner, run = self.execute(fake)
        eid = runner.create_experiment(["N01"], "model-minimal", agent_kind="model")
        second = runner.execute_experiment(eid)
        first = self.store.experiment_info(run["experiment_id"])
        self.assertTrue(compare(first, second)["comparable"])
        second["runs"][0]["metadata"]["reported_models"] = ["provider-changed-model"]
        self.assertFalse(compare(first, second)["comparable"])
        second["runs"][0]["metadata"]["reported_models"] = ["mock-model"]
        second["config"]["model"] = "different-model"
        result = compare(first, second)
        self.assertFalse(result["comparable"])
        self.assertIsNone(result["contract_rate_delta"])

    def test_configuration_has_no_secret_field_and_validates_budgets(self):
        path = Path(self.temp.name) / "model.json"
        public = save_model_config(self.config.to_dict(), path)
        self.assertTrue(public["configured"])
        self.assertNotIn("fixture-credential", path.read_text(encoding="utf-8"))
        self.assertEqual(load_model_config(path), self.config)
        with self.assertRaises(ValueError): save_model_config(dict(self.config.to_dict(), api_key="secret"), path)
        for values in ({"max_input_tokens": True}, {"max_output_tokens_per_response": 15}, {"request_timeout_seconds": float("nan")}):
            with self.assertRaises(ValueError): replace(self.config, **values)
        self.assertEqual(len(TOOLS), 3)
        self.assertNotIn("recoverable", instructions("model-recovery", "1.4.0", Budget()))


if __name__ == "__main__":
    unittest.main()
