"""Tests for the in-memory parts of the jobs service.

DB-dependent paths (claim_next_job, mark_*, retry / state transitions)
are exercised in M4 end-to-end. M3 tests just lock in the validation
contract so a typo in JobType doesn't ship.
"""

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.jobs as jobs

    importlib.reload(jobs)
    return jobs


def test_valid_job_types_match_schema(_setup_env):
    """The hardcoded service set must match the pydantic Literal."""
    jobs = _setup_env
    from app.schemas.jobs import JobType
    import typing

    schema_values = set(typing.get_args(JobType))
    assert schema_values == jobs.VALID_JOB_TYPES, (
        "JobType literal in schemas/jobs.py drifted from "
        "VALID_JOB_TYPES in services/jobs.py — keep them in sync."
    )


def test_terminal_statuses_subset_of_status_literal(_setup_env):
    jobs = _setup_env
    from app.schemas.jobs import JobStatus
    import typing

    schema_values = set(typing.get_args(JobStatus))
    assert jobs.TERMINAL_STATUSES <= schema_values
