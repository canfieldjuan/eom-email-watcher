"""TOML string encoding for public test and qualification fixtures."""

from tomlkit import string


def toml_path_literal(path) -> str:
    """Let the existing TOML serializer own escaping; never quote raw paths."""
    return string(str(path)).as_string()
