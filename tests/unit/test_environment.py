from __future__ import annotations

import platform

from backend.benchmarking.environment import capture_environment


def test_environment_fingerprint_fields() -> None:
    env = capture_environment(workload="w", size="small", source_tree_sha256="abc")
    for key in (
        "os",
        "os_release",
        "architecture",
        "logical_cpus",
        "python_version",
        "python_implementation",
        "packages",
        "captured_at",
        "notes",
    ):
        assert key in env
    assert env["python_version"] == platform.python_version()
    assert env["workload"] == "w"
    assert env["source_tree_sha256"] == "abc"
    assert env["packages"]["numpy"]
    assert isinstance(env["uv_lock_sha256"], str)
    assert len(env["uv_lock_sha256"]) == 64
    assert any("thermal" in note for note in env["notes"])


def test_uncontrolled_noise_sources_are_noted() -> None:
    env = capture_environment()
    notes = " ".join(env["notes"])
    assert "affinity" in notes
    assert "frequency" in notes
    if env["cpu_affinity"] is None:
        assert "CPU affinity unavailable" in notes
