from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ssh_mcp.ssh import glob_to_regex


class GlobToRegexTests(unittest.TestCase):
    def _assert_matches(self, pattern: str, candidate: str) -> None:
        regex = glob_to_regex(pattern)
        self.assertIsNotNone(
            regex.fullmatch(candidate),
            f"expected pattern {pattern!r} to match {candidate!r}",
        )

    def _assert_no_match(self, pattern: str, candidate: str) -> None:
        regex = glob_to_regex(pattern)
        self.assertIsNone(
            regex.fullmatch(candidate),
            f"expected pattern {pattern!r} to NOT match {candidate!r}",
        )

    def test_star_matches_within_single_segment_only(self) -> None:
        self._assert_matches("*.py", "foo.py")
        self._assert_no_match("*.py", "sub/foo.py")

    def test_star_does_not_match_hidden_files(self) -> None:
        self._assert_no_match("*.py", ".hidden.py")
        self._assert_matches("*", "foo")
        self._assert_no_match("*", ".env")

    def test_dot_prefixed_pattern_matches_hidden_files(self) -> None:
        self._assert_matches(".env", ".env")
        self._assert_matches(".*", ".env")

    def test_question_mark_matches_exactly_one_character(self) -> None:
        self._assert_matches("a?c.txt", "abc.txt")
        self._assert_no_match("a?c.txt", "ac.txt")
        self._assert_no_match("a?c.txt", "abbc.txt")

    def test_double_star_matches_zero_or_more_segments(self) -> None:
        self._assert_matches("**/*.py", "foo.py")
        self._assert_matches("**/*.py", "sub/foo.py")
        self._assert_matches("**/*.py", "a/b/c.py")

    def test_double_star_skips_hidden_directories(self) -> None:
        self._assert_no_match("**/*.py", ".hidden/foo.py")
        self._assert_no_match("**/*.py", "sub/.foo.py")

    def test_double_star_in_the_middle_of_pattern(self) -> None:
        self._assert_matches("src/**/*.ts", "src/foo.ts")
        self._assert_matches("src/**/*.ts", "src/a/b/c.ts")
        self._assert_no_match("src/**/*.ts", "other/foo.ts")
        self._assert_no_match("src/**/*.ts", "src/.git/config.ts")

    def test_double_star_at_end_matches_everything_under_prefix(self) -> None:
        self._assert_matches("src/**", "src/foo.txt")
        self._assert_matches("src/**", "src/a/b.txt")
        self._assert_no_match("src/**", "other/foo.txt")

    def test_bare_double_star_matches_any_non_hidden_path(self) -> None:
        self._assert_matches("**", "foo.txt")
        self._assert_matches("**", "a/b/c.txt")
        self._assert_no_match("**", ".git/config")

    def test_multiple_double_stars(self) -> None:
        self._assert_matches("**/foo/**", "a/foo/b/c.txt")
        self._assert_matches("**/foo/**", "foo/b.txt")
        self._assert_no_match("**/foo/**", "foo")

    def test_brace_alternation(self) -> None:
        self._assert_matches("*.{ts,tsx}", "foo.ts")
        self._assert_matches("*.{ts,tsx}", "foo.tsx")
        self._assert_no_match("*.{ts,tsx}", "foo.js")

    def test_brace_alternation_spanning_a_whole_segment(self) -> None:
        self._assert_matches("{src,lib}/*.ts", "src/a.ts")
        self._assert_matches("{src,lib}/*.ts", "lib/a.ts")
        self._assert_no_match("{src,lib}/*.ts", "test/a.ts")

    def test_character_class(self) -> None:
        self._assert_matches("[abc].txt", "a.txt")
        self._assert_no_match("[abc].txt", "d.txt")

    def test_negated_character_class(self) -> None:
        self._assert_matches("[!abc].txt", "d.txt")
        self._assert_no_match("[!abc].txt", "a.txt")
        self._assert_matches("[^abc].txt", "d.txt")

    def test_literal_special_regex_characters_are_escaped(self) -> None:
        self._assert_matches("a.b.txt", "a.b.txt")
        self._assert_no_match("a.b.txt", "aXb.txt")


if __name__ == "__main__":
    unittest.main()
