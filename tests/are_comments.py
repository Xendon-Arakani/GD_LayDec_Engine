from __future__ import annotations

import ast
import io
from pathlib import Path
import tokenize


ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "build", "tmp", "artifacts", "data"}
DOC_TYPES = {".md", ".rst", ".adoc"}
PY_TYPES = {".py"}
SLASH_TYPES = {".c", ".cc", ".cpp", ".h", ".hh", ".hpp"}
HASH_TYPES = {".cmake", ".toml", ".ps1", ".sh", ".yaml", ".yml"}
FAIL_TEXT = "Comments aren't allowed. Documentation outside explicitly required data saving is not allowed."


def files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return found


def py_has_comment(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    if any(token.type == tokenize.COMMENT for token in tokens):
        return True
    tree = ast.parse(text)
    bodies = [tree]
    bodies.extend(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    for node in bodies:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return True
    return False


def slash_comment(text: str) -> bool:
    state = "code"
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index:index + 2]
        if state == "code":
            if pair in {"//", "/*"}:
                return True
            if char == '"':
                state = "string"
            elif char == "'":
                state = "char"
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == '"':
                state = "code"
        elif state == "char":
            if char == "\\":
                index += 1
            elif char == "'":
                state = "code"
        index += 1
    return False


def hash_comment(text: str) -> bool:
    for line in text.splitlines():
        quoted = False
        escaped = False
        for char in line:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                quoted = not quoted
                continue
            if char == "#" and not quoted:
                return True
    return False


def main() -> None:
    bad: list[str] = []
    all_files = files()
    test_files = [path for path in all_files if path.parent == ROOT / "tests"]
    if test_files != [Path(__file__).resolve()]:
        bad.extend(str(path) for path in test_files)
    for path in all_files:
        suffix = path.suffix.lower()
        if suffix in DOC_TYPES:
            bad.append(str(path))
            continue
        try:
            if suffix in PY_TYPES and py_has_comment(path):
                bad.append(str(path))
            elif suffix in SLASH_TYPES and slash_comment(path.read_text(encoding="utf-8")):
                bad.append(str(path))
            elif suffix in HASH_TYPES and hash_comment(path.read_text(encoding="utf-8")):
                bad.append(str(path))
        except (OSError, UnicodeError, SyntaxError, tokenize.TokenError):
            bad.append(str(path))
    if bad:
        raise SystemExit(FAIL_TEXT + "\n" + "\n".join(sorted(set(bad))))


if __name__ == "__main__":
    main()
