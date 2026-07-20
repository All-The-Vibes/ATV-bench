"""Focused tests for the dependency-injected model-backed CLI wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import scripts.run_model_backed_eval as cli
from atv_bench.operator import (
    GatewayEndpointContract,
    ModelBackedOperatorError,
    ProviderBindings,
)
from atv_bench.sandbox import CliOciEngine


def _required_args(*, factory_module: str) -> list[str]:
    return [
        "--policy",
        "policy.json",
        "--task",
        "task",
        "--harness",
        "phoenix",
        "--harness",
        "hve",
        "--out",
        "output",
        "--backend-factory",
        f"{factory_module}:backend_factory",
        "--gateway-endpoint-factory",
        f"{factory_module}:gateway_endpoint_factory",
        "--engine",
        "docker",
    ]


def test_cli_curries_engine_server_and_policy_into_endpoint_factory(
    monkeypatch,
    capsys,
    tmp_path,
):
    observed: dict[str, object] = {}
    policy = SimpleNamespace(
        network_name="atv-private",
        gateway_identity="gateway.internal",
    )
    plan = SimpleNamespace(policy=policy)
    engine = CliOciEngine("docker")
    server = SimpleNamespace(host="127.0.0.1", port=43123)
    module_name = "_atv_test_model_backed_factories"
    module = ModuleType(module_name)

    def backend_factory(received_policy):
        observed["backend_policy"] = received_policy
        return ProviderBindings(
            backends={"provider-a": object()},
            credentials={"provider-a": "secret"},
        )

    def gateway_endpoint_factory(received_engine, received_server, received_policy):
        observed["endpoint_args"] = (
            received_engine,
            received_server,
            received_policy,
        )
        return GatewayEndpointContract(
            network_name=received_policy.network_name,
            host=received_policy.gateway_identity,
            port=443,
            tls=True,
            healthcheck=lambda _handle, _request: None,
        )

    module.backend_factory = backend_factory
    module.gateway_endpoint_factory = gateway_endpoint_factory
    monkeypatch.setitem(sys.modules, module_name, module)

    class FakePlanLoader:
        @classmethod
        def load(cls, **_kwargs):
            return plan

    class FakeOperator:
        def __init__(
            self,
            *,
            providers,
            oci_runner_factory,
            gateway_endpoint_factory,
        ):
            observed["providers"] = providers
            observed["runner_factory"] = oci_runner_factory
            self.gateway_endpoint_factory = gateway_endpoint_factory

        def run(self, received_plan, output):
            endpoint = self.gateway_endpoint_factory(server, received_plan.policy)
            endpoint.validate_for(received_plan.policy)
            observed["endpoint"] = endpoint
            observed["runner"] = observed["runner_factory"](tmp_path / "work", server)
            return SimpleNamespace(
                root=Path(output),
                succeeded=True,
                plan_digest="a" * 64,
                schedule_id="schedule",
                attempts=(),
                trust_tier="local-self-attested",
                rankable=False,
                official_verified=False,
            )

    runner = object()

    def fake_runner_factory(received_engine, *, work_root):
        observed["runner_args"] = (received_engine, work_root)
        return runner

    monkeypatch.setattr(cli, "ModelBackedEvalPlan", FakePlanLoader)
    monkeypatch.setattr(cli, "ModelBackedOperator", FakeOperator)
    monkeypatch.setattr(cli, "_engine", lambda _name: engine)
    monkeypatch.setattr(cli, "OciTrialRunner", fake_runner_factory)

    assert cli.main(_required_args(factory_module=module_name)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert observed["backend_policy"] is policy
    assert observed["endpoint_args"] == (engine, server, policy)
    assert isinstance(observed["endpoint"], GatewayEndpointContract)
    assert observed["runner"] is runner
    assert observed["runner_args"] == (engine, tmp_path / "work")


def test_cli_requires_gateway_endpoint_factory(capsys):
    args = _required_args(factory_module="unused")
    option = args.index("--gateway-endpoint-factory")
    del args[option : option + 2]

    with pytest.raises(SystemExit) as caught:
        cli._parser().parse_args(args)

    assert caught.value.code == 2
    assert "--gateway-endpoint-factory" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("symbol", "code"),
    [
        ("missing-colon", "gateway_endpoint_factory_invalid"),
        (
            "_atv_missing_gateway_factory:factory",
            "gateway_endpoint_factory_unavailable",
        ),
    ],
)
def test_cli_rejects_unloadable_gateway_endpoint_factories(symbol, code):
    with pytest.raises(ModelBackedOperatorError) as caught:
        cli._load_gateway_endpoint_factory(symbol)

    assert caught.value.code == code


def test_cli_rejects_noncallable_or_wrong_signature_gateway_factories(monkeypatch):
    module_name = "_atv_test_invalid_gateway_factories"
    module = ModuleType(module_name)
    module.not_callable = object()
    module.wrong_signature = lambda _engine, _server: None
    monkeypatch.setitem(sys.modules, module_name, module)

    for attribute in ("not_callable", "wrong_signature"):
        with pytest.raises(ModelBackedOperatorError) as caught:
            cli._load_gateway_endpoint_factory(f"{module_name}:{attribute}")
        assert caught.value.code == "gateway_endpoint_factory_invalid"


def test_cli_reports_invalid_gateway_factory_as_structured_error(
    monkeypatch,
    capsys,
):
    module_name = "_atv_test_cli_invalid_gateway_factory"
    module = ModuleType(module_name)
    module.backend_factory = lambda _policy: None
    module.gateway_endpoint_factory = object()
    monkeypatch.setitem(sys.modules, module_name, module)

    assert cli.main(_required_args(factory_module=module_name)) == 2

    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "error"
    assert payload["code"] == "gateway_endpoint_factory_invalid"
    assert payload["rankable"] is False
    assert payload["official_verified"] is False


def test_cli_rejects_gateway_factory_result_with_wrong_type():
    engine = CliOciEngine("docker")
    bound = cli._bind_gateway_endpoint_factory(
        lambda _engine, _server, _policy: object(),
        engine,
    )

    with pytest.raises(ModelBackedOperatorError) as caught:
        bound(SimpleNamespace(), SimpleNamespace())

    assert caught.value.code == "gateway_endpoint_factory_result_invalid"
