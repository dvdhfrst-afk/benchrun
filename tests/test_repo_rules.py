"""Keeps the repository portable and honest about what it needs."""

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKIP_FOLDERS = {".git", "__pycache__", ".venv", "venv"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".sh", ".txt", ""}
ALLOWED_IMPORTS = {
    "argparse", "ast", "collections", "contextlib", "copy", "csv", "dataclasses", "datetime", "hashlib", "html",
    "inspect", "io", "itertools", "json", "math", "operator", "os", "pathlib", "random", "re", "select", "shutil",
    "socket", "sqlite3", "string", "subprocess", "sys", "tempfile", "threading", "time", "typing", "unittest",
    "xml", "benchrun", "tests", "__future__",
}


def repo_files():
    for path in sorted(ROOT.rglob("*")):
        parts = path.relative_to(ROOT).parts
        generated = parts[0] == "reports"
        if path.is_file() and not generated and not (set(parts) & SKIP_FOLDERS):
            yield path


def python_files():
    return [path for path in repo_files() if path.suffix == ".py"]


class Sources(unittest.TestCase):
    def test_every_source_parses_as_python_39(self):
        files = python_files()
        self.assertGreater(len(files), 20)
        for path in files:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                ast.parse(source, filename=str(path), feature_version=(3, 9))

    def test_only_the_standard_library_is_imported(self):
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                for name in names:
                    self.assertIn(name.split(".")[0], ALLOWED_IMPORTS, f"{path}: {name}")

    def test_text_files_have_no_long_dashes_and_use_lf(self):
        long_dashes = (chr(0x2013), chr(0x2014))
        for path in repo_files():
            if path.suffix not in TEXT_SUFFIXES:
                continue
            data = path.read_bytes()
            self.assertNotIn(b"\r\n", data, f"{path} must use LF line endings")
            text = data.decode("utf-8")
            for dash in long_dashes:
                self.assertNotIn(dash, text, str(path))

    def test_gitignore_cannot_hide_source_folders(self):
        """A bare folder pattern matches at any depth, including inside the package."""
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        patterns = {line[:-1] for line in lines if line.endswith("/") and not line.startswith("/")}
        for path in ROOT.rglob("*.py"):
            folders = set(path.relative_to(ROOT).parts[:-1])
            self.assertFalse(folders & patterns - SKIP_FOLDERS, path)

    def test_python_sources_are_plain_ascii(self):
        for path in python_files():
            path.read_bytes().decode("ascii")

    def test_readme_never_chains_shell_commands(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        inside = False
        for line in text.splitlines():
            if line.startswith("```"):
                inside = not inside
            elif inside:
                for token in ("&&", "||", "; "):
                    self.assertNotIn(token, line, line)

    def test_simulators_listen_only_on_loopback(self):
        source = (ROOT / "benchrun" / "sim" / "server.py").read_text(encoding="utf-8")
        self.assertIn('LOOPBACK = "127.0.0.1"', source)
        self.assertNotIn("0.0.0.0", source)


class WorkedFixture(unittest.TestCase):
    def test_expected_values_can_be_regenerated_without_the_package(self):
        script = ROOT / "tests" / "fixtures" / "work_out_expected.py"
        tree = ast.parse(script.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }
        self.assertEqual(imported, {"json", "math", "sys"})
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "expected.json"
            subprocess.run(
                [sys.executable, "-I", str(script), str(out)],
                check=True,
                stdout=subprocess.DEVNULL,
                cwd=folder,
            )
            fresh = out.read_bytes()
        committed = (ROOT / "tests" / "fixtures" / "verdict_expected.json").read_bytes()
        self.assertEqual(fresh, committed)


if __name__ == "__main__":
    unittest.main()
