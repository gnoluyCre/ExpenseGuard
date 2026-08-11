"""ORM metadata checks for CP-F8.1 grading snapshots."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.sql.schema import Constraint

from app.db.base import Base


def _constraint_names(table_name: str, kind: type[Constraint]) -> set[str]:
    return {
        str(item.name)
        for item in Base.metadata.tables[table_name].constraints
        if isinstance(item, kind) and item.name is not None
    }


def test_grading_orm_exposes_five_immutable_snapshot_tables() -> None:
    for name in (
        "grading_config",
        "grading_run",
        "grading_request",
        "grading_item",
        "grading_item_row",
    ):
        assert name in Base.metadata.tables
        assert "tenant_id" in Base.metadata.tables[name].columns


def test_grading_orm_closes_run_config_and_source_identities() -> None:
    assert "uq_grading_config_snapshot" in _constraint_names("grading_config", UniqueConstraint)
    assert "fk_grading_run_config_snapshot" in _constraint_names(
        "grading_run", ForeignKeyConstraint
    )
    for name in (
        "fk_grading_item_run_identity",
        "fk_grading_item_finding_identity",
        "fk_grading_item_correlation_identity",
        "fk_grading_item_capability_snapshot",
        "fk_grading_item_investigation_identity",
        "fk_grading_item_investigation_result_snapshot",
    ):
        assert name in _constraint_names("grading_item", ForeignKeyConstraint)


def test_grading_item_orm_has_branch_json_and_partial_unique_guards() -> None:
    checks = _constraint_names("grading_item", CheckConstraint)
    assert {
        "ck_grading_item_source_fields_consistent",
        "ck_grading_item_f7_terminal_consistent",
        "ck_grading_item_reason_codes_valid",
        "ck_grading_item_evidence_size",
        "ck_grading_item_impact_range",
        "ck_grading_item_confidence_range",
    } <= checks
    indexes = {index.name: index for index in Base.metadata.tables["grading_item"].indexes}
    for name in (
        "uq_grading_item_deterministic_source",
        "uq_grading_item_correlation_source",
    ):
        assert isinstance(indexes[name], Index)
        assert indexes[name].unique is True
        assert indexes[name].dialect_options["postgresql"]["where"] is not None


def test_grading_item_row_orm_uses_physical_composite_identities() -> None:
    foreign_keys = _constraint_names("grading_item_row", ForeignKeyConstraint)
    uniques = _constraint_names("grading_item_row", UniqueConstraint)
    assert "fk_grading_item_row_item_identity" in foreign_keys
    assert "fk_grading_item_row_expense_identity" in foreign_keys
    assert "uq_grading_item_row_item_ordinal" in uniques
    assert "uq_grading_item_row_item_row" in uniques
