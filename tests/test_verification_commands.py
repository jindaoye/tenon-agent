"""Check recognition and conservative pytest coverage without executing shell text."""

from __future__ import annotations

import pytest

from backend.runtime.execution.verification_commands import is_verification_command, pytest_scope


@pytest.mark.parametrize(
    "command",
    [
        "PYTHONPATH=lib pytest -q tests/test_urls.py",
        "PYTHONPATH=lib HOME='/tmp/home dir' EMPTY= pytest tests/test_urls.py",
        'env PYTHONPATH="lib folder" pytest -q',
        "env -i --unset=HOME -u DISPLAY -- PYTHONPATH=lib python -m pytest -q",
        "git status; PYTHONPATH=lib pytest -q",
        "$env:PYTHONPATH='lib'; python -m pytest -q",
        "gcc main.c -o /tmp/main",
        "g++ main.cpp -o /tmp/main",
        "clang++ main.cpp -o /tmp/main",
        "clang main.c -o /tmp/main",
        "rustc main.rs -o /tmp/main",
    ],
)
def test_environment_prefixes_and_compilers_are_checks(command: str) -> None:
    assert is_verification_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "env PYTHONPATH=lib rg pytest README.md",
        "PYTHONPATH=lib echo pytest",
        "echo 'text; pytest -q'",
        'printf "instructions\npytest -q"',
        "grep 'pytest' README.md",
        "gcc --version | head -1",
        "rustc --version",
        "clang --help",
        "MYVALUE=" + "a" * 20000 + " echo pytest",
    ],
)
def test_mentions_of_checks_are_not_invocations(command: str) -> None:
    assert not is_verification_command(command)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("pytest -q tests/test_a.py -k feature", "pytest -vv tests/test_a.py"),
        ("pytest tests/test_a.py::test_feature", "pytest tests/test_a.py tests/test_b.py"),
        ("pytest ./tests/test_a.py -m slow", "pytest tests/test_a.py"),
        ("pytest -k feature", "pytest -q"),
        ("PYTHONPATH=lib pytest tests/test_a.py -k feature", "PYTHONPATH=lib pytest tests/test_a.py"),
        ("pytest --keyword=feature tests/test_a.py", "pytest tests/test_a.py -k feature"),
    ],
)
def test_success_can_cover_the_same_or_broader_explicit_selection(before: str, after: str) -> None:
    old_scope, new_scope = pytest_scope(before), pytest_scope(after)
    assert old_scope is not None and new_scope is not None
    assert new_scope.covers(old_scope)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("pytest tests/test_a.py", "pytest tests/test_b.py"),
        ("pytest tests/test_a.py", "pytest tests/test_a.py -k feature"),
        ("pytest tests/test_a.py -k first", "pytest tests/test_a.py -k second"),
        ("PYTHONPATH=lib pytest tests/test_a.py", "PYTHONPATH=other pytest tests/test_a.py"),
        ("pytest tests/test_a.py", "python -m pytest tests/test_a.py"),
        ("pytest tests/test_a.py", "pytest tests"),
        ("pytest tests/test_a.py", "pytest"),
        ("pytest", "pytest ."),
        ("pytest .", "pytest"),
    ],
)
def test_uncertain_or_narrower_coverage_does_not_supersede_a_check(before: str, after: str) -> None:
    old_scope, new_scope = pytest_scope(before), pytest_scope(after)
    assert old_scope is not None and new_scope is not None
    assert not new_scope.covers(old_scope)


@pytest.mark.parametrize(
    "command",
    [
        "pytest --collect-only tests/test_a.py",
        "pytest --lf tests/test_a.py",
        "pytest --ignore=tests/other tests/test_a.py",
        "pytest -c other.ini tests/test_a.py",
        "pytest -o addopts= tests/test_a.py",
        "pytest -k",
        "pytest -k 'unterminated",
        "cd /app && pytest tests/test_a.py",
        "pytest tests/test_a.py; echo done",
        "pytest $TEST_FILE",
        "PYTHONPATH=$LIB pytest tests/test_a.py",
    ],
)
def test_unknown_selection_and_compound_commands_have_no_inferred_scope(command: str) -> None:
    assert pytest_scope(command) is None
