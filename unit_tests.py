import asyncio
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def _make_sqs_record(agent_id: str, bedrock_agent_id: str, message_id: str = "msg-1") -> dict:
    return {
        "messageId": message_id,
        "receiptHandle": "rh-1",
        "body": json.dumps({"agent_id": agent_id, "bedrock_agent_id": bedrock_agent_id}),
    }


def _make_stream_record(event_name: str, old_image: dict | None = None) -> dict:
    record: dict = {"eventName": event_name, "dynamodb": {}}
    if old_image is not None:
        # Minimal DynamoDB wire format (all strings for simplicity)
        record["dynamodb"]["OldImage"] = {
            k: {"S": str(v)} for k, v in old_image.items()
        }
    return record


# ---------------------------------------------------------------------------
# TestModelSelector
# ---------------------------------------------------------------------------

class TestModelSelector(unittest.TestCase):
    def _run(self, **kwargs):
        import deployweave_mcp as m
        return run(m.model_selector(**kwargs))

    def test_latency_500_returns_haiku(self):
        result = self._run(task_type="classification", latency_budget_ms=500)
        import deployweave_mcp as m
        self.assertEqual(result["selected_model"], m.MODEL_HAIKU)

    def test_latency_499_returns_haiku(self):
        result = self._run(task_type="classification", latency_budget_ms=499)
        import deployweave_mcp as m
        self.assertEqual(result["selected_model"], m.MODEL_HAIKU)

    def test_latency_501_does_not_fast_path(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(task_type="reasoning", latency_budget_ms=501, ab_mode="winner")
        # Falls back to Sonnet when no metrics
        self.assertEqual(result["selected_model"], m.MODEL_SONNET)

    def test_invalid_ab_mode_raises(self):
        with self.assertRaises(ValueError):
            self._run(task_type="classification", ab_mode="bogus")

    def test_ab_mode_winner_picks_highest_score(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        call_count = [0]

        def fake_query(**kwargs):
            call_count[0] += 1
            # Calls are in CANDIDATE_MODELS order: Haiku, Sonnet, Opus
            # Return good metrics only on the second call (Sonnet)
            if call_count[0] == 2:
                return {"Items": [{"success_rate": "0.9", "accuracy_score": "0.95", "task_type": "reasoning"}]}
            return {"Items": []}

        mock_table.query.side_effect = fake_query
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(task_type="reasoning", latency_budget_ms=1000, ab_mode="winner")
        self.assertEqual(result["selected_model"], m.MODEL_SONNET)
        self.assertEqual(result["ab_mode"], "winner")

    def test_ab_mode_winner_empty_metrics_fallback_sonnet(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(task_type="planning", latency_budget_ms=800, ab_mode="winner")
        self.assertEqual(result["selected_model"], m.MODEL_SONNET)
        self.assertIn("No historical metrics", result["reasoning"])

    def test_ab_mode_split_returns_primary_and_secondary(self):
        import deployweave_mcp as m
        result = self._run(task_type="content_generation", latency_budget_ms=800, ab_mode="split")
        self.assertEqual(result["primary_model"], m.MODEL_SONNET)
        self.assertEqual(result["secondary_model"], m.MODEL_HAIKU)
        self.assertIn("split_ratio", result)

    def test_ab_mode_split_ratio_clamped(self):
        result = self._run(task_type="content_generation", latency_budget_ms=800,
                           ab_mode="split", ab_split_ratio=1.5)
        self.assertEqual(result["split_ratio"]["primary"], 1.0)

    def test_ab_mode_metric_returns_candidates(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(task_type="classification", latency_budget_ms=800, ab_mode="metric")
        self.assertIn("candidates", result)
        self.assertEqual(len(result["candidates"]), 3)

    def test_estimated_cost_present(self):
        result = self._run(task_type="classification", latency_budget_ms=400)
        self.assertIn("estimated_cost_usd", result)
        self.assertIsInstance(result["estimated_cost_usd"], float)


# ---------------------------------------------------------------------------
# TestTeamProvisioner
# ---------------------------------------------------------------------------

class TestTeamProvisioner(unittest.TestCase):
    def _make_bedrock_resp(self, agent_id: str = "BID1", arn: str = "arn:aws:bedrock:us-east-1:123:agent/BID1"):
        resp = {"agent": {"agentId": agent_id, "agentArn": arn, "agentStatus": "CREATING"}}
        return resp

    def test_empty_specs_raises(self):
        import deployweave_mcp as m
        with self.assertRaises(ValueError):
            run(m.team_provisioner(agent_specs=[]))

    def test_specs_capped_at_5(self):
        import deployweave_mcp as m
        specs = [{"name": f"agent{i}", "system_prompt": "test", "model": m.MODEL_HAIKU} for i in range(10)]
        mock_table = MagicMock()
        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            return self._make_bedrock_resp(f"BID{call_count[0]}", f"arn::{call_count[0]}")

        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.create_agent.side_effect = fake_create
            mock_ddb.return_value.Table.return_value = mock_table
            result = run(m.team_provisioner(agent_specs=specs))

        self.assertEqual(result["total_agents_provisioned"], 5)
        self.assertEqual(call_count[0], 5)

    def test_provisioned_agents_have_bedrock_fields(self):
        import deployweave_mcp as m
        specs = [{"name": "worker", "system_prompt": "do work", "model": m.MODEL_SONNET}]
        mock_table = MagicMock()
        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.create_agent.return_value = self._make_bedrock_resp()
            mock_ddb.return_value.Table.return_value = mock_table
            result = run(m.team_provisioner(agent_specs=specs))

        agent = result["provisioned_agents"][0]
        self.assertIn("bedrock_agent_id", agent)
        self.assertIn("bedrock_agent_arn", agent)
        self.assertIn("expires_at", agent)

    def test_rollback_on_partial_failure(self):
        import deployweave_mcp as m
        specs = [
            {"name": "a1", "system_prompt": "p1", "model": m.MODEL_HAIKU},
            {"name": "a2", "system_prompt": "p2", "model": m.MODEL_HAIKU},
            {"name": "a3", "system_prompt": "p3", "model": m.MODEL_HAIKU},
        ]
        mock_table = MagicMock()
        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise _client_error("ServiceQuotaExceededException")
            return self._make_bedrock_resp(f"BID{call_count[0]}", f"arn::{call_count[0]}")

        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.create_agent.side_effect = fake_create
            mock_ba.return_value.delete_agent.return_value = {}
            mock_ddb.return_value.Table.return_value = mock_table
            with self.assertRaises(ClientError):
                run(m.team_provisioner(agent_specs=specs))
            # 2 successful agents should have been rolled back
            self.assertEqual(mock_ba.return_value.delete_agent.call_count, 2)

    def test_ttl_stored_in_dynamodb(self):
        import deployweave_mcp as m
        specs = [{"name": "x", "system_prompt": "y", "model": m.MODEL_HAIKU}]
        mock_table = MagicMock()
        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.create_agent.return_value = self._make_bedrock_resp()
            mock_ddb.return_value.Table.return_value = mock_table
            run(m.team_provisioner(agent_specs=specs, ttl_hours=72))

        put_item_call = mock_table.put_item.call_args
        item = put_item_call[1]["Item"]
        self.assertIn("ttl", item)
        # ttl should be roughly now + 72h
        expected_ttl = int(time.time()) + 72 * 3600
        self.assertAlmostEqual(item["ttl"], expected_ttl, delta=5)


# ---------------------------------------------------------------------------
# TestAdapterResolver
# ---------------------------------------------------------------------------

class TestAdapterResolver(unittest.TestCase):
    def _run(self, **kwargs):
        import deployweave_mcp as m
        return run(m.adapter_resolver(**kwargs))

    def test_invalid_operation_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="explode")

    def test_list_adapters_no_task_type_scans(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [{"adapter_id": "a1"}]}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="list_adapters")
        mock_table.scan.assert_called_once()
        self.assertEqual(result["count"], 1)

    def test_list_adapters_with_task_type_queries_gsi(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            self._run(operation="list_adapters", task_type="classification")
        mock_table.query.assert_called_once()
        call_kwargs = mock_table.query.call_args[1]
        self.assertEqual(call_kwargs["IndexName"], "task_type-index")

    def test_get_adapter_returns_item(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"adapter_id": "ax"}}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="get_adapter", adapter_id="ax")
        self.assertEqual(result["adapters"][0]["adapter_id"], "ax")

    def test_get_adapter_not_found_raises(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            with self.assertRaises(ValueError):
                self._run(operation="get_adapter", adapter_id="missing")

    def test_get_adapter_missing_id_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="get_adapter")

    def test_register_adapter_generates_uuid(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(
                operation="register_adapter",
                adapter_metadata={"base_model": m.MODEL_HAIKU, "s3_path": "s3://b/f.safetensors", "tags": ["cs"]},
            )
        self.assertIn("adapter_id", result)
        self.assertTrue(result["adapter_id"])
        mock_table.put_item.assert_called_once()

    def test_register_adapter_missing_s3_path_raises(self):
        import deployweave_mcp as m
        with self.assertRaises(ValueError):
            self._run(
                operation="register_adapter",
                adapter_metadata={"base_model": m.MODEL_HAIKU},
            )

    def test_register_adapter_missing_metadata_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="register_adapter")

    def test_search_by_tags_empty_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="search_by_tags", tags=[])

    def test_search_by_tags_single_queries_gsi(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [{"primary_tag": "cs", "tags": {"cs"}}]}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="search_by_tags", tags=["cs"])
        mock_table.query.assert_called_once()
        self.assertEqual(result["count"], 1)

    def test_search_by_tags_multiple_filters_client_side(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [
                {"primary_tag": "cs", "tags": {"cs", "sentiment"}},
                {"primary_tag": "cs", "tags": {"cs"}},
            ]
        }
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="search_by_tags", tags=["cs", "sentiment"])
        # Only the first item has both tags
        self.assertEqual(result["count"], 1)


# ---------------------------------------------------------------------------
# TestAgentLifecycle
# ---------------------------------------------------------------------------

class TestAgentLifecycle(unittest.TestCase):
    def _run(self, **kwargs):
        import deployweave_mcp as m
        return run(m.agent_lifecycle(**kwargs))

    def _make_bedrock_resp(self, aid="BID1", arn="arn::1"):
        return {"agent": {"agentId": aid, "agentArn": arn}}

    def test_invalid_operation_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="fly")

    def test_provision_missing_name_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="provision", system_prompt="x")

    def test_provision_missing_prompt_raises(self):
        with self.assertRaises(ValueError):
            self._run(operation="provision", agent_name="x")

    def test_provision_creates_agent_and_stores(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.create_agent.return_value = self._make_bedrock_resp()
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="provision", agent_name="myagent", system_prompt="do stuff")
        self.assertEqual(result["status"], "provisioned")
        self.assertEqual(result["bedrock_agent_id"], "BID1")
        mock_table.put_item.assert_called_once()

    def test_provision_ttl_set_correctly(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.create_agent.return_value = self._make_bedrock_resp()
            mock_ddb.return_value.Table.return_value = mock_table
            run(m.agent_lifecycle(operation="provision", agent_name="x",
                                  system_prompt="y", ttl_hours=48))
        item = mock_table.put_item.call_args[1]["Item"]
        expected = int(time.time()) + 48 * 3600
        self.assertAlmostEqual(item["ttl"], expected, delta=5)

    def test_list_agents_classifies_active_expired(self):
        import deployweave_mcp as m
        now = int(time.time())
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [
            {"agent_id": "a1", "expiry_timestamp": now + 3600, "created_at": now, "name": "x", "model": m.MODEL_HAIKU},
            {"agent_id": "a2", "expiry_timestamp": now - 3600, "created_at": now, "name": "y", "model": m.MODEL_HAIKU},
        ]}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="list_agents")
        self.assertEqual(result["active_agents"], 1)
        self.assertEqual(result["expired_agents"], 1)

    def test_get_agent_returns_item(self):
        import deployweave_mcp as m
        now = int(time.time())
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"agent_id": "a1", "expiry_timestamp": now + 3600}
        }
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="get_agent", agent_id="a1")
        self.assertEqual(result["status"], "active")

    def test_get_agent_expired_status(self):
        import deployweave_mcp as m
        now = int(time.time())
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"agent_id": "a1", "expiry_timestamp": now - 100}
        }
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="get_agent", agent_id="a1")
        self.assertEqual(result["status"], "expired")

    def test_get_agent_not_found_raises(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            with self.assertRaises(ValueError):
                self._run(operation="get_agent", agent_id="missing")

    def test_delete_agent_calls_bedrock_and_dynamo(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"agent_id": "a1", "bedrock_agent_id": "BID1"}
        }
        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="delete_agent", agent_id="a1")
        self.assertEqual(result["status"], "deleted")
        mock_ba.return_value.delete_agent.assert_called_once()
        mock_table.delete_item.assert_called_once_with(Key={"agent_id": "a1"})

    def test_delete_agent_404_is_idempotent(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {"agent_id": "a1", "bedrock_agent_id": "BID1"}
        }
        with patch.object(m, "get_bedrock_agents") as mock_ba, \
             patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.delete_agent.side_effect = _client_error("ResourceNotFoundException")
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="delete_agent", agent_id="a1")
        self.assertEqual(result["status"], "deleted")

    def test_extend_ttl_updates_table(self):
        import deployweave_mcp as m
        mock_table = MagicMock()
        with patch.object(m, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value = mock_table
            result = self._run(operation="extend_ttl", agent_id="a1", ttl_hours=24)
        self.assertEqual(result["status"], "ttl_extended")
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        expected_ttl = int(time.time()) + 24 * 3600
        actual_ttl = call_kwargs["ExpressionAttributeValues"][":ttl"]
        self.assertAlmostEqual(actual_ttl, expected_ttl, delta=5)


# ---------------------------------------------------------------------------
# TestCleanupLambda
# ---------------------------------------------------------------------------

class TestCleanupLambda(unittest.TestCase):
    def _invoke(self, records):
        import cleanup_lambda as cl
        return cl.lambda_handler({"Records": records}, None)

    def test_successful_cleanup(self):
        import cleanup_lambda as cl
        records = [_make_sqs_record("a1", "BID1")]
        with patch.object(cl, "get_bedrock_agents") as mock_ba, \
             patch.object(cl, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
            result = self._invoke(records)
        self.assertEqual(result["body"]["successful_cleanups"], 1)
        self.assertEqual(result["body"]["failed_cleanups"], 0)
        self.assertEqual(result["batchItemFailures"], [])
        mock_ba.return_value.delete_agent.assert_called_once_with(
            agentId="BID1", skipResourceInUseCheck=True
        )

    def test_bedrock_404_is_success(self):
        import cleanup_lambda as cl
        records = [_make_sqs_record("a1", "BID1")]
        with patch.object(cl, "get_bedrock_agents") as mock_ba, \
             patch.object(cl, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.delete_agent.side_effect = _client_error("ResourceNotFoundException")
            mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
            result = self._invoke(records)
        self.assertEqual(result["body"]["successful_cleanups"], 1)
        self.assertEqual(result["batchItemFailures"], [])

    def test_bedrock_other_error_fails_record(self):
        import cleanup_lambda as cl
        records = [_make_sqs_record("a1", "BID1", message_id="msg-fail")]
        with patch.object(cl, "get_bedrock_agents") as mock_ba, \
             patch.object(cl, "get_dynamodb"):
            mock_ba.return_value.delete_agent.side_effect = _client_error("ThrottlingException")
            result = self._invoke(records)
        self.assertEqual(result["body"]["failed_cleanups"], 1)
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "msg-fail"}])

    def test_dynamodb_failure_fails_record(self):
        import cleanup_lambda as cl
        records = [_make_sqs_record("a1", "BID1", message_id="msg-ddb")]
        with patch.object(cl, "get_bedrock_agents"), \
             patch.object(cl, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = \
                _client_error("ProvisionedThroughputExceededException")
            result = self._invoke(records)
        self.assertEqual(result["body"]["failed_cleanups"], 1)
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "msg-ddb"}])

    def test_bedrock_deleted_before_dynamo(self):
        """Agents are deleted before DynamoDB; a DynamoDB failure does not prevent Bedrock deletion."""
        import cleanup_lambda as cl
        records = [_make_sqs_record("a1", "BID1")]
        call_order = []
        with patch.object(cl, "get_bedrock_agents") as mock_ba, \
             patch.object(cl, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.delete_agent.side_effect = lambda **kw: call_order.append("bedrock")
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = \
                lambda **kw: (_ for _ in ()).throw(_client_error("InternalServerError"))
            self._invoke(records)
        self.assertIn("bedrock", call_order)

    def test_malformed_json_fails_record(self):
        import cleanup_lambda as cl
        bad_record = {"messageId": "msg-bad", "body": "not json", "receiptHandle": "rh"}
        result = self._invoke([bad_record])
        self.assertEqual(result["body"]["failed_cleanups"], 1)

    def test_partial_batch_failure(self):
        import cleanup_lambda as cl
        records = [
            _make_sqs_record("a1", "BID1", message_id=f"msg-{i}") for i in range(10)
        ]
        call_count = [0]

        def alternate_fail(**kw):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise _client_error("ThrottlingException")

        with patch.object(cl, "get_bedrock_agents") as mock_ba, \
             patch.object(cl, "get_dynamodb") as mock_ddb:
            mock_ba.return_value.delete_agent.side_effect = alternate_fail
            mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
            result = self._invoke(records)
        self.assertEqual(result["body"]["successful_cleanups"], 5)
        self.assertEqual(result["body"]["failed_cleanups"], 5)
        self.assertEqual(len(result["batchItemFailures"]), 5)

    def test_response_shape(self):
        import cleanup_lambda as cl
        records = [_make_sqs_record("a1", "BID1")]
        with patch.object(cl, "get_bedrock_agents"), \
             patch.object(cl, "get_dynamodb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
            result = self._invoke(records)
        self.assertIn("statusCode", result)
        self.assertIn("body", result)
        self.assertIn("batchItemFailures", result)
        self.assertEqual(result["statusCode"], 200)


# ---------------------------------------------------------------------------
# TestStreamsToSQS
# ---------------------------------------------------------------------------

class TestStreamsToSQS(unittest.TestCase):
    def _invoke(self, records):
        import streams_to_sqs as s
        return s.lambda_handler({"Records": records}, None)

    def test_remove_event_sends_sqs(self):
        import streams_to_sqs as s
        records = [_make_stream_record("REMOVE", {"agent_id": "a1", "bedrock_agent_id": "BID1"})]
        with patch.object(s, "get_sqs") as mock_sqs:
            result = self._invoke(records)
        self.assertEqual(result["sent_to_sqs"], 1)
        self.assertEqual(result["skipped"], 0)
        mock_sqs.return_value.send_message.assert_called_once()

    def test_insert_event_skipped(self):
        import streams_to_sqs as s
        records = [_make_stream_record("INSERT")]
        with patch.object(s, "get_sqs") as mock_sqs:
            result = self._invoke(records)
        self.assertEqual(result["sent_to_sqs"], 0)
        self.assertEqual(result["skipped"], 1)
        mock_sqs.return_value.send_message.assert_not_called()

    def test_modify_event_skipped(self):
        import streams_to_sqs as s
        records = [_make_stream_record("MODIFY")]
        with patch.object(s, "get_sqs"):
            result = self._invoke(records)
        self.assertEqual(result["skipped"], 1)

    def test_remove_without_old_image_skipped(self):
        import streams_to_sqs as s
        records = [{"eventName": "REMOVE", "dynamodb": {}}]
        with patch.object(s, "get_sqs") as mock_sqs:
            result = self._invoke(records)
        self.assertEqual(result["skipped"], 1)
        mock_sqs.return_value.send_message.assert_not_called()

    def test_sqs_message_contains_agent_id(self):
        import streams_to_sqs as s
        records = [_make_stream_record("REMOVE", {"agent_id": "a99", "bedrock_agent_id": "BIDX"})]
        with patch.object(s, "get_sqs") as mock_sqs:
            self._invoke(records)
        call_kwargs = mock_sqs.return_value.send_message.call_args[1]
        body = json.loads(call_kwargs["MessageBody"])
        self.assertEqual(body["agent_id"], "a99")
        self.assertEqual(body["bedrock_agent_id"], "BIDX")

    def test_sqs_send_failure_does_not_raise(self):
        import streams_to_sqs as s
        records = [_make_stream_record("REMOVE", {"agent_id": "a1", "bedrock_agent_id": "B1"})]
        with patch.object(s, "get_sqs") as mock_sqs:
            mock_sqs.return_value.send_message.side_effect = Exception("SQS down")
            # Should not raise; just logs and skips
            result = self._invoke(records)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["sent_to_sqs"], 0)

    def test_missing_bedrock_id_in_old_image_skips(self):
        import streams_to_sqs as s
        records = [_make_stream_record("REMOVE", {"agent_id": "a1"})]  # no bedrock_agent_id
        with patch.object(s, "get_sqs") as mock_sqs:
            result = self._invoke(records)
        self.assertEqual(result["skipped"], 1)
        mock_sqs.return_value.send_message.assert_not_called()

    def test_mixed_event_types(self):
        import streams_to_sqs as s
        records = [
            _make_stream_record("INSERT"),
            _make_stream_record("REMOVE", {"agent_id": "a1", "bedrock_agent_id": "B1"}),
            _make_stream_record("MODIFY"),
            _make_stream_record("REMOVE", {"agent_id": "a2", "bedrock_agent_id": "B2"}),
        ]
        with patch.object(s, "get_sqs") as mock_sqs:
            result = self._invoke(records)
        self.assertEqual(result["sent_to_sqs"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(mock_sqs.return_value.send_message.call_count, 2)

    def test_deserializer_handles_string_type(self):
        import streams_to_sqs as s
        item = s.deserialize_dynamodb_item({
            "agent_id": {"S": "abc"},
            "bedrock_agent_id": {"S": "XYZ"},
        })
        self.assertEqual(item["agent_id"], "abc")
        self.assertEqual(item["bedrock_agent_id"], "XYZ")

    def test_response_contains_counts(self):
        import streams_to_sqs as s
        with patch.object(s, "get_sqs"):
            result = self._invoke([])
        self.assertIn("sent_to_sqs", result)
        self.assertIn("skipped", result)
        self.assertEqual(result["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
