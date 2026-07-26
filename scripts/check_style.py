from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINE_LIMIT = 119
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
DIRECTIVE_RE = re.compile(
    r"^(?:noqa|type:\s*ignore|fmt:|pragma:|pylint:|mypy:)",
    re.IGNORECASE,
)
DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
DOCSTRING_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
STRING_TOKEN_TYPES = {tokenize.STRING}
for token_name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    token_type = getattr(tokenize, token_name, None)
    if token_type is not None:
        STRING_TOKEN_TYPES.add(token_type)


def definition_start(node: ast.AST) -> int:
    lines = [node.lineno]
    lines.extend(decorator.lineno for decorator in getattr(node, "decorator_list", []))
    return min(lines)


def associated_comment_start(lines: list[str], start_line: int) -> int:
    index = start_line - 2
    while index >= 0 and lines[index].lstrip().startswith("#"):
        index -= 1
    return index + 2


def blank_lines_before(lines: list[str], start_line: int) -> int:
    index = start_line - 2
    count = 0
    while index >= 0 and not lines[index].strip():
        count += 1
        index -= 1
    return count


def first_letter(text: str) -> str:
    match = LETTER_RE.search(text)
    return match.group(0) if match else ""


def collect_token_metadata(
    text: str,
) -> tuple[list[tokenize.TokenInfo], set[int], dict[int, int]]:
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    multiline_strings: set[int] = set()
    literal_lengths: dict[int, int] = {}

    for token in tokens:
        if token.type == tokenize.STRING and token.end[0] > token.start[0]:
            multiline_strings.update(range(token.start[0], token.end[0] + 1))
        if token.type in STRING_TOKEN_TYPES:
            literal_lengths[token.start[0]] = (
                literal_lengths.get(token.start[0], 0) + len(token.string)
            )

    return tokens, multiline_strings, literal_lengths


def check_comments(
    path: Path,
    tokens: list[tokenize.TokenInfo],
    errors: list[str],
) -> None:
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue

        body = token.string[1:].strip()
        location = f"{path.relative_to(ROOT)}:{token.start[0]}"
        if not body or DIRECTIVE_RE.match(body):
            continue
        if not CYRILLIC_RE.search(body):
            errors.append(f"{location}: комментарий должен быть на русском языке")

        letter = first_letter(body)
        if letter and letter.isupper():
            errors.append(f"{location}: комментарий должен начинаться со строчной буквы")
        if body.endswith("."):
            errors.append(f"{location}: комментарий не должен заканчиваться точкой")

        prefix = token.line[: token.start[1]]
        if prefix.strip() and not prefix.endswith("  "):
            errors.append(
                f"{location}: перед встроенным комментарием нужны два пробела"
            )


def check_docstrings(
    path: Path,
    tree: ast.AST,
    errors: list[str],
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, DOCSTRING_NODES) or not getattr(node, "body", None):
            continue
        value = ast.get_docstring(node, clean=False)
        if value is None:
            continue

        line_number = node.body[0].lineno
        location = f"{path.relative_to(ROOT)}:{line_number}"
        meaningful_lines = [line.strip() for line in value.splitlines() if line.strip()]
        if not meaningful_lines:
            errors.append(f"{location}: docstring не должен быть пустым")
            continue
        if not CYRILLIC_RE.search(value):
            errors.append(f"{location}: docstring должен быть на русском языке")

        letter = first_letter(meaningful_lines[0])
        if letter and letter.isupper():
            errors.append(f"{location}: docstring должен начинаться со строчной буквы")
        if any(line.endswith(".") for line in meaningful_lines):
            errors.append(f"{location}: строки docstring не должны заканчиваться точкой")


def check_definition_spacing(
    path: Path,
    lines: list[str],
    tree: ast.Module,
    errors: list[str],
) -> None:
    for node in tree.body:
        if isinstance(node, DEFINITION_NODES):
            start = associated_comment_start(lines, definition_start(node))
            if blank_lines_before(lines, start) < 2:
                errors.append(
                    f"{path.relative_to(ROOT)}:{start}: "
                    "перед верхнеуровневым определением нужны две пустые строки"
                )

        if isinstance(node, ast.ClassDef):
            for child_index, child in enumerate(node.body):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if child_index == 0:
                    continue
                start = associated_comment_start(lines, definition_start(child))
                if blank_lines_before(lines, start) < 1:
                    errors.append(
                        f"{path.relative_to(ROOT)}:{start}: "
                        "между методами нужна пустая строка"
                    )


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    relative_path = path.relative_to(ROOT)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    try:
        tree = ast.parse(text, filename=str(relative_path))
        tokens, multiline_strings, literal_lengths = collect_token_metadata(text)
    except (SyntaxError, tokenize.TokenError) as error:
        return [f"{relative_path}: синтаксическая ошибка: {error}"]

    if text and not text.endswith("\n"):
        errors.append(f"{relative_path}: файл должен заканчиваться переводом строки")

    for line_number, line in enumerate(lines, 1):
        location = f"{relative_path}:{line_number}"
        if "\t" in line:
            errors.append(f"{location}: табуляция запрещена")
        if line.rstrip() != line:
            errors.append(f"{location}: найден хвостовой пробел")
        if (
            len(line) > LINE_LIMIT
            and line_number not in multiline_strings
            and literal_lengths.get(line_number, 0) <= 96
        ):
            errors.append(
                f"{location}: строка длиннее {LINE_LIMIT} символов без длинного литерала"
            )

    for token in tokens:
        if token.type == tokenize.INDENT and len(token.string.expandtabs(4)) % 4:
            errors.append(
                f"{relative_path}:{token.start[0]}: отступ должен быть кратен четырём"
            )
        if token.type == tokenize.OP and token.string == ";":
            errors.append(
                f"{relative_path}:{token.start[0]}: точка с запятой в Python-коде запрещена"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and len(node.names) > 1:
            errors.append(
                f"{relative_path}:{node.lineno}: каждый import должен быть на отдельной строке"
            )
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            errors.append(
                f"{relative_path}:{node.lineno}: bare except запрещён"
            )

    check_comments(path, tokens, errors)
    check_docstrings(path, tree, errors)
    check_definition_spacing(path, lines, tree, errors)
    return errors


def main() -> int:
    python_files = sorted(
        path
        for path in ROOT.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
    )
    errors = [
        error
        for path in python_files
        for error in check_file(path)
    ]

    if errors:
        print("\n".join(errors))
        print(f"\nнайдено нарушений: {len(errors)}")
        return 1

    print(
        f"стиль проверен: {len(python_files)} файлов, "
        "нарушений pep-8 и текстовых правил нет"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
