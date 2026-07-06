from __future__ import annotations


class FormulaError(ValueError):
    """Base class for stable, source-located formula diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        line: int,
        column: int,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.line = line
        self.column = column
        self.end_line = end_line if end_line is not None else line
        self.end_column = end_column if end_column is not None else column


class FormulaSyntaxError(FormulaError):
    """The source is outside the supported, non-executable grammar."""

    def __init__(
        self,
        message: str,
        *,
        line: int,
        column: int,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> None:
        super().__init__(
            message,
            code="formula_syntax_error",
            line=line,
            column=column,
            end_line=end_line,
            end_column=end_column,
        )


class FormulaLimitError(FormulaError):
    """Parsing stopped because a public resource limit was exceeded."""

    def __init__(
        self,
        *,
        limit: str,
        maximum: int,
        line: int = 1,
        column: int = 1,
    ) -> None:
        super().__init__(
            f"Formula exceeds the {limit} limit of {maximum}.",
            code="formula_limit_exceeded",
            line=line,
            column=column,
        )
        self.limit = limit
        self.maximum = maximum
