"""Parsing a unified diff into the document clients render.

Line numbers are what a review comment anchors to, so they are the thing worth pinning down.
"""
import subprocess

from review_mate.view.diffdoc import ADDED, CONTEXT, REMOVED, build, parse, split_files

SAMPLE = """@@ -46,3 +46,4 @@ def reserve(self, pu):
     def reserve(self, pu):
-        if pu.legacy:
+        if pu.fleet == LEGACY:
+            q = self._legacy
         return q.take(pu.size)
"""


def test_line_numbers_advance_per_side():
    hunk = parse(SAMPLE)[0]
    assert [(line.side, line.old, line.new) for line in hunk.lines] == [
        (CONTEXT, 46, 46),
        (REMOVED, 47, None),
        (ADDED, None, 47),
        (ADDED, None, 48),
        (CONTEXT, 48, 49),
    ]


def test_the_header_is_parsed_whole():
    hunk = parse(SAMPLE)[0]
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (46, 3, 46, 4)
    assert hunk.heading == "def reserve(self, pu):"


def test_a_trailing_newline_does_not_become_a_line():
    assert len(parse(SAMPLE)[0].lines) == 5          # not 6: the split's empty tail is not a line


def test_gap_before_measures_the_unshown_lines():
    two = SAMPLE + "@@ -100,1 +101,1 @@\n context\n"
    first, second = parse(two)
    assert first.gap_before == 45                     # lines 1..45 are not shown
    assert second.gap_before == 101 - (46 + 4)        # between the end of hunk one and hunk two


def test_a_single_line_hunk_header_omits_its_count():
    hunk = parse("@@ -7 +7 @@\n-a\n+b\n")[0]
    assert (hunk.old_count, hunk.new_count) == (1, 1)
    assert [line.side for line in hunk.lines] == [REMOVED, ADDED]


def test_file_headers_and_no_newline_markers_are_not_body():
    text = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n"
            "@@ -1,1 +1,1 @@\n-a\n+b\n\\ No newline at end of file\n")
    hunk = parse(text)[0]
    assert [line.text for line in hunk.lines] == ["a", "b"]


def test_tokens_land_on_the_right_side():
    hunk = build(SAMPLE, "capacity.py")[0]
    added = [line for line in hunk.lines if line.side == ADDED]
    assert all(line.tokens for line in added)
    assert any(kind == "keyword" for line in added for _, _, kind in line.tokens)


def test_every_hunk_in_real_history_matches_its_declared_counts():
    """The parser against real git output rather than hand-written samples."""
    revs = subprocess.run(["git", "rev-list", "-40", "HEAD"],
                          capture_output=True, text=True).stdout.split()
    checked = 0
    for rev in revs:
        paths = subprocess.run(["git", "show", "--format=", "--name-only", rev],
                               capture_output=True, text=True).stdout.split()
        # parse() takes one file's diff, so ask git per path rather than slicing a combined
        # stream on "diff --git" — a string that also occurs inside this repo's own test data
        texts = [subprocess.run(["git", "show", "--format=", "--unified=3", rev, "--", path],
                                capture_output=True, text=True).stdout for path in paths]
        for hunk in (h for text in texts for h in parse(text)):
            old = sum(1 for line in hunk.lines if line.side in (CONTEXT, REMOVED))
            new = sum(1 for line in hunk.lines if line.side in (CONTEXT, ADDED))
            assert (old, new) == (hunk.old_count, hunk.new_count), (rev, hunk.old_start)
            checked += 1
    assert checked > 50, f"only {checked} hunks exercised"


# --- splitting a multi-file diff --------------------------------------------

def test_content_containing_a_file_header_does_not_start_a_file():
    """The reason the splitter tracks hunk bounds instead of matching on the header string."""
    text = (
        "diff --git a/doc.md b/doc.md\n--- a/doc.md\n+++ b/doc.md\n"
        "@@ -1,2 +1,3 @@\n"
        " an example of the format:\n"
        "+diff --git a/fake.py b/fake.py\n"
        " end\n"
    )
    files = split_files(text)
    assert [path for path, _ in files] == ["doc.md"]
    assert "fake.py" in files[0][1]            # it stayed inside doc.md's hunk, as content


def test_an_added_empty_file_is_named_from_its_header():
    text = ("diff --git a/pkg/__init__.py b/pkg/__init__.py\n"
            "new file mode 100644\nindex 0000000..e69de29\n")
    assert [path for path, _ in split_files(text)] == ["pkg/__init__.py"]


def test_a_deletion_is_named_from_the_old_side():
    text = ("diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
            "--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n")
    assert [path for path, _ in split_files(text)] == ["gone.py"]


def test_each_split_file_parses_on_its_own():
    text = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -5,1 +5,2 @@\n c\n+d\n")
    files = split_files(text)
    assert [path for path, _ in files] == ["a.py", "c.py"]
    second = parse(files[1][1])[0]
    assert second.new_start == 5 and [line.side for line in second.lines] == [CONTEXT, ADDED]


def test_splitting_real_history_matches_git(tmp_path):
    revs = subprocess.run(["git", "rev-list", "-60", "HEAD"],
                          capture_output=True, text=True).stdout.split()
    for rev in revs:
        expected = subprocess.run(["git", "show", "--format=", "--name-only", rev],
                                  capture_output=True, text=True).stdout.split()
        text = subprocess.run(["git", "show", "--format=", "--unified=3", rev],
                              capture_output=True, text=True).stdout
        assert [path for path, _ in split_files(text)] == expected, rev
