from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "monitor" / "server.py"


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_mobile_sleep_endpoint_cannot_reach_oauth_or_fitbit_http() -> None:
    endpoint = _function("api_mobile_sleep_projection")
    reader = _function("_read_mobile_sleep_projection")
    names = {
        node.id
        for function in (endpoint, reader)
        for node in ast.walk(function)
        if isinstance(node, ast.Name)
    }

    assert names.isdisjoint(
        {
            "valid_tokens",
            "refresh_tokens",
            "_build_sleep_report_payload",
            "req",
            "requests",
        }
    )


def test_existing_background_recovery_fetch_owns_projection_refresh() -> None:
    recovery = _function("_get_sleep_recovery_signal")
    calls = [
        node.func.id
        for node in ast.walk(recovery)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert calls.count("_build_sleep_report_payload") == 1
    assert calls.count("_write_mobile_sleep_projection") == 1


def test_failed_sleep_fetch_cannot_replace_the_last_valid_projection() -> None:
    report = _function("_build_sleep_report_payload")
    guard = next(
        node
        for node in ast.walk(report)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Attribute)
        and isinstance(node.test.operand.value, ast.Name)
        and node.test.operand.value.id == "sleep_r"
        and node.test.operand.attr == "ok"
    )

    assert any(isinstance(node, ast.Return) for node in guard.body)
