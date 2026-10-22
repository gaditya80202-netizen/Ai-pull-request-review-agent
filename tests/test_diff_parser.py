from backend.integrations.diff_parser import parse_diff


def test_added_and_modified_lines():
    diff = """\
diff --git a/README.md b/README.md
index 1234567..abcdef0 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 existing line
-old line
+modified replacement
+new line
"""
    assert parse_diff(diff) == {"README.md": {2, 3}}


def test_deletions_do_not_create_new_line_numbers():
    diff = """\
diff --git a/example.py b/example.py
index 1234567..abcdef0 100644
--- a/example.py
+++ b/example.py
@@ -1,3 +1,2 @@
 first
-removed line
 third
"""
    assert parse_diff(diff) == {}


def test_multiple_hunks():
    diff = """\
diff --git a/example.py b/example.py
index 1234567..abcdef0 100644
--- a/example.py
+++ b/example.py
@@ -1,2 +1,3 @@
 first
-old second
+new second
+extra
@@ -20,1 +21,2 @@
 context
+another addition
"""
    assert parse_diff(diff) == {"example.py": {2, 3, 22}}


def test_renamed_file_uses_new_path():
    diff = """\
diff --git a/old_name.py b/new_name.py
similarity index 80%
rename from old_name.py
rename to new_name.py
--- a/old_name.py
+++ b/new_name.py
@@ -1 +1,2 @@
-old
+new
+extra
"""
    assert parse_diff(diff) == {"new_name.py": {1, 2}}


def test_deleted_file_has_no_reviewable_added_lines():
    diff = """\
diff --git a/removed.py b/removed.py
deleted file mode 100644
--- a/removed.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old first
-old second
"""
    assert parse_diff(diff) == {}


def test_file_without_patch_is_omitted():
    diff = """\
diff --git a/image.png b/image.png
similarity index 100%
rename from image.png
rename to assets/image.png
"""
    assert parse_diff(diff) == {}


def test_added_source_line_starting_with_plus_sign():
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1,0 +2,1 @@
+++ this is source code
"""
    assert parse_diff(diff) == {"example.py": {2}}
