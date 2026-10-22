from __future__ import annotations

from backend.runtime.execution.verification_diagnostics import changed_existing_tests, failure_excerpt


def test_failure_excerpt_preserves_call_mismatch_and_final_test_id() -> None:
    output = (
        "noise\n" * 30
        + "E   AssertionError: expected call not found.\n"
        + "E   Expected: open(option=True)\nE   Actual: open()\n"
        + "FAILED tests/test_api.py::test_wrapper - AssertionError\n1 failed, 2 passed\n"
    )
    excerpt = "\n".join(failure_excerpt(output))
    assert "Expected: open(option=True)" in excerpt
    assert "Actual: open()" in excerpt
    assert "tests/test_api.py::test_wrapper" in excerpt
    assert "noise" not in excerpt


def test_failure_excerpt_is_bounded_deduplicated_and_redacted_before_truncation() -> None:
    secret = "sk-" + "a" * 40
    lines = [f"E   AssertionError: {secret} {'x' * 500} {index}" for index in range(20)]
    excerpt = failure_excerpt("\x1b[31m" + "\n".join(lines) + "\x1b[0m")
    assert len(excerpt) <= 8
    assert all(len(line) <= 240 for line in excerpt)
    assert secret not in "\n".join(excerpt)
    assert "\x1b" not in "\n".join(excerpt)
    assert failure_excerpt(None) == ()


def test_existing_test_changes_are_distinguished_from_additions_and_production_edits() -> None:
    patch = """diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -1 +1 @@
-    assert call == expected
+    assert call == new_expected
diff --git a/tests/test_new.py b/tests/test_new.py
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+def test_new(): pass
diff --git a/src/api.py b/src/api.py
--- a/src/api.py
+++ b/src/api.py
-    return old
+    return new
"""
    assert changed_existing_tests(patch) == [("tests/test_api.py", "assert call == expected")]


def test_deleted_tests_are_visible_but_comment_and_whitespace_changes_are_not() -> None:
    patch = """diff --git a/tests/test_deleted.py b/tests/test_deleted.py
--- a/tests/test_deleted.py
+++ /dev/null
@@ -1 +0,0 @@
-def test_deleted(): pass
diff --git a/tests/test_comments.py b/tests/test_comments.py
--- a/tests/test_comments.py
+++ b/tests/test_comments.py
-# comment
-
+# new comment
"""
    assert changed_existing_tests(patch) == [("tests/test_deleted.py", "def test_deleted(): pass")]
