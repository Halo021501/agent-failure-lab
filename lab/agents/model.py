"""Responses function loop. Decisions and invalid calls belong to the model."""
import copy
import json
import time
from ..common import canonical, digest, Halt, LabError
from ..model_protocol import TOOLS, instructions


def visible_payload(payload):
    visible = copy.deepcopy(payload)
    visible["input"] = [item for item in visible["input"] if item.get("type") != "reasoning"]
    return visible


def visible_output(items):
    visible = []
    for item in items:
        if not isinstance(item, dict):
            raise LabError("model_protocol_error", "模型 output 包含无效元素")
        if item.get("type") == "function_call":
            visible.append({key: item.get(key) for key in ("type", "id", "call_id", "name", "arguments", "status")})
        elif item.get("type") == "message":
            content = item.get("content", [])
            if not isinstance(content, list):
                raise LabError("model_protocol_error", "模型可见消息无效")
            visible.append({"type": "message", "role": item.get("role"), "content": [
                {key: part[key] for key in ("type", "text", "refusal") if key in part}
                for part in content if isinstance(part, dict) and part.get("type") in ("output_text", "refusal")]})
    return visible


class ModelAgent:
    def __init__(self, strategy, config, transport, stats, progress=None):
        self.strategy, self.config, self.transport, self.stats = strategy, config, transport, stats
        self.progress = progress

    def execute(self, gateway, target):
        config, stats = self.config, self.stats
        history = [{"role": "user", "content": "请读取资料并提交发布核查报告。"}]
        seen_ids, write_ack = set(), False
        deadline = gateway.started + gateway.budget.wall_seconds

        def progress(step):
            if self.progress:
                self.progress(step)

        while True:
            gateway.check()
            if stats["model_calls"] >= gateway.budget.max_model_calls or stats["network_requests"] + 2 > config.max_network_requests:
                raise Halt("budget_exceeded", "模型或网络请求次数预算已耗尽")
            if not gateway.store.model_budget_available(gateway.run_id, config):
                raise Halt("budget_exceeded", "本批模型预算已耗尽")
            cap = min(config.max_output_tokens_per_response, config.max_output_tokens - stats["reserved_output_tokens"])
            if cap < 16:
                raise Halt("budget_exceeded", "模型输出 Token 预算已耗尽")
            payload = {"model": config.model, "instructions": instructions(self.strategy, target, gateway.budget),
                       "input": history, "tools": TOOLS, "parallel_tool_calls": False, "store": False,
                       "include": ["reasoning.encrypted_content"], "truncation": "disabled", "max_output_tokens": cap}
            request_hash = digest(payload)
            stats["network_requests"] += 1
            stats["token_count_requests"] += 1
            progress("counting_tokens")
            gateway.event("model_token_count_request", {"request": visible_payload(payload), "request_sha256": request_hash})
            try:
                counted = self.transport.count(payload, gateway.cancel, deadline)
            except LabError as error:
                gateway.event("model_error", {"phase": "token_count", "code": error.code})
                raise
            except Exception as error:
                gateway.event("model_error", {"phase":"token_count", "code":"model_transport_error"})
                raise LabError("model_transport_error", "输入 Token 预检异常；未发起生成请求") from error
            if not isinstance(counted, dict) or type(counted.get("input_tokens")) is not int or counted["input_tokens"] < 0:
                raise LabError("model_protocol_error", "输入 Token 预检结果无效，未发起生成请求")
            input_count = counted["input_tokens"]
            gateway.event("model_token_count", {"input_tokens": input_count, "request_sha256": request_hash})
            gateway.check()
            if stats["reserved_input_tokens"] + input_count > config.max_input_tokens:
                raise Halt("budget_exceeded", "模型输入 Token 预算不足，未发起生成请求")
            gateway.store.reserve_model_budget(gateway.run_id, input_count, cap, config)
            stats["reserved_input_tokens"] += input_count
            stats["reserved_output_tokens"] += cap
            stats["model_calls"] += 1
            stats["network_requests"] += 1
            number = stats["model_calls"]
            gateway.event("model_request", {"model_call": number, "request": visible_payload(payload),
                                             "request_sha256": request_hash, "reserved_input_tokens": input_count,
                                             "reserved_output_tokens": cap})
            progress("waiting_model")
            started = time.perf_counter()
            try:
                response = self.transport.respond(payload, gateway.cancel, deadline)
            except Exception as error:
                stats["usage_unknown_calls"] += 1
                gateway.event("model_error", {"phase": "generation", "model_call": number,
                                               "code": getattr(error, "code", "model_transport_error"),
                                               "usage": "unknown", "seconds": time.perf_counter() - started})
                if isinstance(error, (Halt, LabError)):
                    raise
                raise LabError("model_transport_error", "模型传输异常，未自动重试") from error
            if not isinstance(response, dict):
                stats["usage_unknown_calls"] += 1
                raise LabError("model_protocol_error", "模型响应不是对象；用量未知")
            if isinstance(response.get("model"), str) and response["model"]:
                stats["reported_models"] = sorted(set(stats.get("reported_models", [])) | {response["model"]})
            usage = response.get("usage")
            usage_valid = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
            if usage_valid:
                actual_input, actual_output = usage["input_tokens"], usage["output_tokens"]
                stats["input_tokens"] += actual_input
                stats["output_tokens"] += actual_output
                stats["reserved_input_tokens"] += max(0, actual_input - input_count)
                stats["reserved_output_tokens"] += actual_output - cap
                gateway.store.settle_model_budget(gateway.run_id, input_count, cap, actual_input, actual_output)
            else:
                stats["usage_unknown_calls"] += 1
            output = response.get("output")
            if not isinstance(output, list):
                raise LabError("model_protocol_error", "模型响应缺少 output 数组")
            gateway.event("model_response", {"model_call": number, "response_id": response.get("id"),
                                              "model": response.get("model"), "status": response.get("status"),
                                              "output": visible_output(output),
                                              "usage": {k: usage[k] for k in ("input_tokens", "output_tokens")} if usage_valid else None,
                                              "usage_unknown": not usage_valid, "seconds": time.perf_counter() - started})
            progress("handling_model_response")
            gateway.check()
            if not usage_valid:
                raise LabError("model_usage_unknown", "模型未返回有效用量，停止后续生成请求")
            if actual_input > input_count or actual_output > cap:
                raise Halt("budget_exceeded", "供应商用量超过预检或响应限制；已记录实际用量并停止")
            if response.get("status") != "completed":
                raise LabError("model_incomplete", "模型响应未完成，未自动重试")
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                if not write_ack:
                    raise LabError("model_no_confirmed_report", "模型结束时没有已确认提交的报告")
                return
            # Opaque reasoning items are carried only in memory, never written to the event log.
            history.extend(output)
            for call in calls:
                gateway.check()
                provider_id, name, arguments = call.get("call_id"), call.get("name"), call.get("arguments")
                if not isinstance(provider_id, str) or not provider_id or provider_id in seen_ids or not isinstance(name, str):
                    raise LabError("model_protocol_error", "工具调用 ID 重复、为空或名称无效")
                seen_ids.add(provider_id)
                try:
                    args = json.loads(arguments, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))) if isinstance(arguments, str) else None
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                except (ValueError, TypeError):
                    args = {"invalid_provider_arguments": arguments}
                gateway.event("model_tool_dispatch", {"provider_call_id": provider_id, "tool_call_id": f"call-{gateway.tool_calls + 1:03d}", "name": name, "arguments": arguments})
                try:
                    result = gateway.call(name, args)
                    tool_output = {"result": result}
                    if name == "write_report":
                        write_ack = True
                except Halt:
                    raise
                except LabError as error:
                    tool_output = {"error": {"code": error.code, "message": str(error)}}
                history.append({"type": "function_call_output", "call_id": provider_id, "output": canonical(tool_output)})
