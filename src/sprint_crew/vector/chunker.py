from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from sprint_crew.tools._safety import FORBIDDEN_PATH_SEGMENTS

INDEXABLE_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".sh",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".txt",
    }
)

SKIP_SUFFIXES = frozenset({".lock", ".min.js", ".map", ".png", ".jpg", ".gif", ".woff", ".woff2"})

MAX_FILE_BYTES = 100_000
CHUNK_CHARS = 1600
CHUNK_OVERLAP = 320
MAX_CHUNK_TEXT_BYTES = 8_192


@dataclass(frozen=True)
class CodeChunk:
    path: str
    start_line: int
    end_line: int
    chunk_kind: str
    language: str
    text: str

    def display_text(self) -> str:
        return (
            f"# path: {self.path}\n"
            f"# lines: {self.start_line}-{self.end_line}\n"
            f"# kind: {self.chunk_kind}\n"
            f"{self.text}"
        )


def _chunk_kind_for_path(rel: str) -> str:
    normalized = rel.replace("\\", "/")
    if normalized.startswith("tests/") or "/tests/" in normalized or normalized.startswith("test_"):
        return "test"
    if normalized.endswith((".md", ".rst")):
        return "doc"
    if normalized.endswith((".yaml", ".yml", ".toml", ".json")):
        return "config"
    return "code"


def _language_for_suffix(suffix: str) -> str:
    return {
        ".py": "python",
        ".md": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".json": "json",
        ".sh": "shell",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".txt": "text",
    }.get(suffix, "text")


def _should_skip_path(rel: Path) -> bool:
    for part in rel.parts:
        if part in FORBIDDEN_PATH_SEGMENTS:
            return True
    suffix = rel.suffix.lower()
    if suffix in SKIP_SUFFIXES:
        return True
    if suffix and suffix not in INDEXABLE_SUFFIXES:
        return False if suffix == "" else True
    return False


def _sliding_window_chunks(
    rel: str,
    content: str,
    *,
    chunk_kind: str,
    language: str,
) -> list[CodeChunk]:
    lines = content.splitlines()
    if not lines:
        return []
    chunks: list[CodeChunk] = []
    start = 0
    while start < len(content):
        end = min(len(content), start + CHUNK_CHARS)
        piece = content[start:end]
        line_start = content[:start].count("\n") + 1
        line_end = line_start + piece.count("\n")
        chunks.append(
            CodeChunk(
                path=rel,
                start_line=line_start,
                end_line=max(line_end, line_start),
                chunk_kind=chunk_kind,
                language=language,
                text=piece[:MAX_CHUNK_TEXT_BYTES],
            )
        )
        if end >= len(content):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def _python_chunks(rel: str, content: str, *, chunk_kind: str) -> list[CodeChunk]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _sliding_window_chunks(rel, content, chunk_kind=chunk_kind, language="python")

    lines = content.splitlines(keepends=True)
    chunks: list[CodeChunk] = []

    def slice_lines(start: int, end: int) -> str:
        return "".join(lines[start - 1 : end])[:MAX_CHUNK_TEXT_BYTES]

    module_doc = ast.get_docstring(tree)
    if module_doc and module_doc.strip():
        end_line = module_doc.count("\n") + 1
        chunks.append(
            CodeChunk(
                path=rel,
                start_line=1,
                end_line=end_line,
                chunk_kind=chunk_kind,
                language="python",
                text=slice_lines(1, end_line),
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            body = slice_lines(node.lineno, end_line)
            if body.strip():
                chunks.append(
                    CodeChunk(
                        path=rel,
                        start_line=node.lineno,
                        end_line=end_line,
                        chunk_kind=chunk_kind,
                        language="python",
                        text=body,
                    )
                )

    if chunks:
        return chunks
    return _sliding_window_chunks(rel, content, chunk_kind=chunk_kind, language="python")


def chunk_file(rel: str, content: str) -> list[CodeChunk]:
    chunk_kind = _chunk_kind_for_path(rel)
    suffix = Path(rel).suffix.lower()
    language = _language_for_suffix(suffix)
    if suffix == ".py":
        return _python_chunks(rel, content, chunk_kind=chunk_kind)
    return _sliding_window_chunks(rel, content, chunk_kind=chunk_kind, language=language)


def iter_workspace_chunks(workspace_root: Path) -> list[CodeChunk]:
    root = workspace_root.resolve()
    all_chunks: list[CodeChunk] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        try:
            rel = file_path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _should_skip_path(Path(rel)):
            continue
        suffix = file_path.suffix.lower()
        if suffix not in INDEXABLE_SUFFIXES:
            continue
        try:
            data = file_path.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES:
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not content.strip():
            continue
        all_chunks.extend(chunk_file(rel, content))
    return all_chunks


def count_indexable_files(workspace_root: Path) -> int:
    root = workspace_root.resolve()
    count = 0
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            rel = Path(file_path.relative_to(root).as_posix())
        except ValueError:
            continue
        if _should_skip_path(rel):
            continue
        if file_path.suffix.lower() not in INDEXABLE_SUFFIXES:
            continue
        count += 1
    return count
