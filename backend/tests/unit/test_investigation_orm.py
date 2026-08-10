"""ORM metadata checks for CP-F7.1 investigation facts."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.sql.schema import Constraint

from app.db.base import Base
from app.db.models import EvidenceStep


def _constraint_names(table_name: str, constraint_type: type[Constraint]) -> set[str]:
    table = Base.metadata.tables[table_name]
    return {
        str(constraint.name)
        for constraint in table.constraints
        if isinstance(constraint, constraint_type) and constraint.name is not None
    }


def test_investigation_orm_exposes_complete_composite_identities() -> None:
    assert "uq_investigation_run_identity" in _constraint_names(
        "investigation_run", UniqueConstraint
    )
    assert "fk_investigation_run_correlation_identity" in _constraint_names(
        "investigation_run", ForeignKeyConstraint
    )
    assert "fk_investigation_request_run_identity" in _constraint_names(
        "investigation_request", ForeignKeyConstraint
    )
    assert "fk_evidence_step_run_identity" in _constraint_names(
        "evidence_step", ForeignKeyConstraint
    )
    assert "fk_investigation_result_run_identity" in _constraint_names(
        "investigation_result", ForeignKeyConstraint
    )


def test_evidence_step_no_longer_points_to_single_row_finding() -> None:
    assert "finding_id" not in EvidenceStep.__table__.columns
    assert "investigation_run_id" in EvidenceStep.__table__.columns
    assert "uq_evidence_step_run_step_no" in _constraint_names("evidence_step", UniqueConstraint)
    assert "ck_evidence_step_action_tool_consistent" in _constraint_names(
        "evidence_step", CheckConstraint
    )


def test_result_and_token_orm_keep_terminal_and_token_guards() -> None:
    assert "ck_investigation_result_outcome_sufficiency_consistent" in _constraint_names(
        "investigation_result", CheckConstraint
    )
    assert "uq_pii_token_source_identity" in _constraint_names("pii_token", UniqueConstraint)
    assert "uq_pii_token_tenant_token" in _constraint_names("pii_token", UniqueConstraint)
