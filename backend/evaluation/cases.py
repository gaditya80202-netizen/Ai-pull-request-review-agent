"""Small, deterministic benchmark for the four review specialists.

These fixtures are synthetic diffs. They are data only and must never
be executed. Each specialist has one known-bug case and one clean control.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    agent: str
    diff: str
    expect_finding: bool
    expected_path: str | None = None
    expected_line: int | None = None


EVALUATION_CASES = [
    EvaluationCase(
        case_id="security_sql_injection",
        agent="security",
        expect_finding=True,
        expected_path="backend/api/users.py",
        expected_line=2,
        diff="""diff --git a/backend/api/users.py b/backend/api/users.py
new file mode 100644
--- /dev/null
+++ b/backend/api/users.py
@@ -0,0 +1,3 @@
+def lookup_user(cursor, username):
+    query = f"SELECT id FROM users WHERE username = '{username}'"
+    cursor.execute(query)
""",
    ),
    EvaluationCase(
        case_id="security_parameterized_sql",
        agent="security",
        expect_finding=False,
        diff="""diff --git a/backend/api/users.py b/backend/api/users.py
new file mode 100644
--- /dev/null
+++ b/backend/api/users.py
@@ -0,0 +1,2 @@
+def lookup_user(cursor, username):
+    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
""",
    ),
    EvaluationCase(
        case_id="quality_wrong_clamp_bound",
        agent="quality",
        expect_finding=True,
        expected_path="backend/utils/numbers.py",
        expected_line=2,
        diff="""diff --git a/backend/utils/numbers.py b/backend/utils/numbers.py
new file mode 100644
--- /dev/null
+++ b/backend/utils/numbers.py
@@ -0,0 +1,2 @@
+def clamp(value: int, low: int, high: int) -> int:
+    return max(low, min(value, low))
""",
    ),
    EvaluationCase(
        case_id="quality_correct_clamp",
        agent="quality",
        expect_finding=False,
        diff="""diff --git a/backend/utils/numbers.py b/backend/utils/numbers.py
new file mode 100644
--- /dev/null
+++ b/backend/utils/numbers.py
@@ -0,0 +1,4 @@
+def clamp(value: int, low: int, high: int) -> int:
+    if low > high:
+        raise ValueError("low must not exceed high")
+    return max(low, min(value, high))
""",
    ),
    EvaluationCase(
        case_id="tests_ineffective_assertion",
        agent="tests",
        expect_finding=True,
        expected_path="tests/test_admin.py",
        expected_line=3,
        diff="""diff --git a/tests/test_admin.py b/tests/test_admin.py
new file mode 100644
--- /dev/null
+++ b/tests/test_admin.py
@@ -0,0 +1,3 @@
+def test_admin_endpoint_rejects_non_admin(non_admin_client):
+    response = non_admin_client.get("/admin")
+    assert response.status_code is not None
""",
    ),
    EvaluationCase(
        case_id="tests_correct_assertion",
        agent="tests",
        expect_finding=False,
        diff="""diff --git a/tests/test_admin.py b/tests/test_admin.py
new file mode 100644
--- /dev/null
+++ b/tests/test_admin.py
@@ -0,0 +1,3 @@
+def test_admin_endpoint_rejects_non_admin(non_admin_client):
+    response = non_admin_client.get("/admin")
+    assert response.status_code == 403
""",
    ),
    EvaluationCase(
        case_id="docs_missing_public_docstring",
        agent="docs",
        expect_finding=True,
        expected_path="backend/api/retry.py",
        expected_line=2,
        diff="""diff --git a/backend/api/retry.py b/backend/api/retry.py
new file mode 100644
--- /dev/null
+++ b/backend/api/retry.py
@@ -0,0 +1,5 @@
+_retry_limit = 3
+def set_retry_limit(limit: int) -> None:
+    global _retry_limit
+    if limit < 1:
+        raise ValueError("limit must be positive")
""",
    ),
    EvaluationCase(
        case_id="docs_documented_public_api",
        agent="docs",
        expect_finding=False,
        diff="""diff --git a/backend/api/retry.py b/backend/api/retry.py
new file mode 100644
--- /dev/null
+++ b/backend/api/retry.py
@@ -0,0 +1,6 @@
+_retry_limit = 3
+def set_retry_limit(limit: int) -> None:
+    "Set the maximum retry attempts for API requests."
+    global _retry_limit
+    if limit < 1:
+        raise ValueError("limit must be positive")
""",
    ),

    EvaluationCase(
        case_id="security_hardcoded_password",
        agent="security",
        expect_finding=True,
        expected_path="backend/config/remote.py",
        expected_line=2,
        diff="""diff --git a/backend/config/remote.py b/backend/config/remote.py
new file mode 100644
--- /dev/null
+++ b/backend/config/remote.py
@@ -0,0 +1,3 @@
+API_ENDPOINT = "https://api.example.com"
+API_PASSWORD = "p@ssword123"
+TIMEOUT_SECONDS = 10
""",
    ),
    EvaluationCase(
        case_id="security_environment_password",
        agent="security",
        expect_finding=False,
        diff="""diff --git a/backend/config/remote.py b/backend/config/remote.py
new file mode 100644
--- /dev/null
+++ b/backend/config/remote.py
@@ -0,0 +1,3 @@
+import os
+API_PASSWORD = os.environ["SERVICE_API_PASSWORD"]
+TIMEOUT_SECONDS = 10
""",
    ),
    EvaluationCase(
        case_id="quality_out_of_range_index",
        agent="quality",
        expect_finding=True,
        expected_path="backend/utils/collection.py",
        expected_line=2,
        diff="""diff --git a/backend/utils/collection.py b/backend/utils/collection.py
new file mode 100644
--- /dev/null
+++ b/backend/utils/collection.py
@@ -0,0 +1,2 @@
+def final_item(items):
+    return items[len(items)]
""",
    ),
    EvaluationCase(
        case_id="quality_safe_final_item",
        agent="quality",
        expect_finding=False,
        diff="""diff --git a/backend/utils/collection.py b/backend/utils/collection.py
new file mode 100644
--- /dev/null
+++ b/backend/utils/collection.py
@@ -0,0 +1,2 @@
+def final_item(items):
+    return items[-1] if items else None
""",
    ),
    EvaluationCase(
        case_id="tests_response_truthiness",
        agent="tests",
        expect_finding=True,
        expected_path="tests/test_routes.py",
        expected_line=3,
        diff="""diff --git a/tests/test_routes.py b/tests/test_routes.py
new file mode 100644
--- /dev/null
+++ b/tests/test_routes.py
@@ -0,0 +1,3 @@
+def test_missing_route(not_found_client):
+    response = not_found_client.get("/not-a-route")
+    assert response
""",
    ),
    EvaluationCase(
        case_id="tests_specific_status_assertion",
        agent="tests",
        expect_finding=False,
        diff="""diff --git a/tests/test_routes.py b/tests/test_routes.py
new file mode 100644
--- /dev/null
+++ b/tests/test_routes.py
@@ -0,0 +1,3 @@
+def test_missing_route(not_found_client):
+    response = not_found_client.get("/not-a-route")
+    assert response.status_code == 404
""",
    ),
    EvaluationCase(
        case_id="docs_inaccurate_admin_comment",
        agent="docs",
        expect_finding=True,
        expected_path="backend/auth.py",
        expected_line=1,
        diff="""diff --git a/backend/auth.py b/backend/auth.py
index 1111111..2222222 100644
--- a/backend/auth.py
+++ b/backend/auth.py
@@ -1,3 +1,3 @@
-# Checks whether the user is an administrator.
+# Returns True when the user is not an administrator.
 def is_admin(user):
     return user.role == "admin"
""",
    ),
    EvaluationCase(
        case_id="docs_accurate_admin_comment",
        agent="docs",
        expect_finding=False,
        diff="""diff --git a/backend/auth.py b/backend/auth.py
index 1111111..2222222 100644
--- a/backend/auth.py
+++ b/backend/auth.py
@@ -1,3 +1,3 @@
-# Checks whether the user is an administrator.
+# Returns True when the user is an administrator.
 def is_admin(user):
     return user.role == "admin"
""",
    ),
]


def validate_evaluation_cases() -> None:
    """Reject malformed cases before any model calls are made."""

    seen_ids = set()

    for case in EVALUATION_CASES:
        if case.case_id in seen_ids:
            raise ValueError(f"Duplicate evaluation case ID: {case.case_id}")
        seen_ids.add(case.case_id)

        if case.agent not in {"security", "quality", "tests", "docs"}:
            raise ValueError(f"Unknown agent in case: {case.case_id}")

        if not case.diff.strip():
            raise ValueError(f"Empty diff in case: {case.case_id}")

        if case.expect_finding:
            if not case.expected_path or not isinstance(
                case.expected_line, int
            ):
                raise ValueError(
                    f"Positive case lacks an expected anchor: {case.case_id}"
                )
        elif (
            case.expected_path is not None
            or case.expected_line is not None
        ):
            raise ValueError(
                f"Clean control unexpectedly has an anchor: {case.case_id}"
            )
