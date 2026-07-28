from app.core.policies.candidates import BindingQuery


def test_binding_query_is_stable_and_contains_only_rule_semantics() -> None:
    first = BindingQuery(
        rule_kind="limit",
        reason_code="LIMIT_EXCEEDED",
        expense_type="交通",
        threshold_semantics="金额大于 500 CNY",
    )
    second = BindingQuery.model_validate(first.model_dump())

    assert first.fingerprint == second.fingerprint
    assert "交通" in first.stable_text()
    assert "employee" not in first.stable_text()
