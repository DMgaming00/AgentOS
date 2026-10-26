"""Safe calculator: structured operations plus a tiny arithmetic expression parser.

The expression grammar is parsed by hand (recursive descent); no ``eval``, no
Python AST execution, no names, no attribute access.
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentos_core.domain.errors import ToolExecutionError
from agentos_core.tools.base import Tool, ToolContext

Operation = Literal[
    "add",
    "subtract",
    "multiply",
    "divide",
    "sum",
    "mean",
    "median",
    "min",
    "max",
    "stdev",
    "percent_change",
    "ratio",
    "evaluate",
]


class CalculatorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Operation = Field(description="Operation to apply.")
    values: list[float] | None = Field(default=None, max_length=1000, description="Operands, in order.")
    expression: str | None = Field(
        default=None,
        max_length=500,
        description="Arithmetic expression with + - * / ^ and parentheses (operation=evaluate).",
    )
    label: str | None = Field(default=None, max_length=200, description="What the number represents.")

    @model_validator(mode="after")
    def _check(self) -> CalculatorArgs:
        if self.operation == "evaluate":
            if not self.expression:
                raise ValueError("evaluate requires 'expression'")
        elif not self.values:
            raise ValueError(f"{self.operation} requires 'values'")
        if self.values and not all(math.isfinite(v) for v in self.values):
            raise ValueError("values must be finite numbers")
        return self


class CalculatorResult(BaseModel):
    operation: str
    result: float
    label: str | None = None


class _Parser:
    MAX_DEPTH = 50

    def __init__(self, text: str) -> None:
        self.tokens: list[str] = []
        pos = 0
        for m in re.finditer(r"\s*((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[-+*/^()])", text):
            if m.start() != pos and text[pos : m.start()].strip():
                raise ToolExecutionError(f"invalid character in expression near {text[pos : m.start()]!r}")
            self.tokens.append(m.group(1))
            pos = m.end()
        if text[pos:].strip():
            raise ToolExecutionError(f"invalid character in expression near {text[pos:]!r}")
        self.i = 0
        self.depth = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.peek()
        if tok is None:
            raise ToolExecutionError("unexpected end of expression")
        self.i += 1
        return tok

    def parse(self) -> float:
        value = self.expr()
        if self.peek() is not None:
            raise ToolExecutionError(f"unexpected token {self.peek()!r}")
        return value

    def expr(self) -> float:
        value = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            rhs = self.term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term(self) -> float:
        value = self.power()
        while self.peek() in ("*", "/"):
            op = self.take()
            rhs = self.power()
            if op == "/" and rhs == 0:
                raise ToolExecutionError("division by zero")
            value = value * rhs if op == "*" else value / rhs
        return value

    def power(self) -> float:
        base = self.unary()
        if self.peek() == "^":
            self.take()
            exp = self.power()
            if abs(exp) > 64:
                raise ToolExecutionError("exponent too large")
            return float(base**exp)
        return base

    def unary(self) -> float:
        if self.peek() in ("+", "-"):
            op = self.take()
            v = self.unary()
            return -v if op == "-" else v
        return self.atom()

    def atom(self) -> float:
        tok = self.take()
        if tok == "(":
            self.depth += 1
            if self.depth > self.MAX_DEPTH:
                raise ToolExecutionError("expression nested too deeply")
            v = self.expr()
            if self.take() != ")":
                raise ToolExecutionError("missing ')'")
            self.depth -= 1
            return v
        try:
            return float(tok)
        except ValueError:
            raise ToolExecutionError(f"unexpected token {tok!r}") from None


def evaluate_expression(text: str) -> float:
    return _Parser(text).parse()


def compute(args: CalculatorArgs) -> float:
    v = args.values or []
    op = args.operation
    if op == "evaluate":
        result = evaluate_expression(args.expression or "")
    elif op in ("add", "sum"):
        result = math.fsum(v)
    elif op == "subtract":
        result = v[0] - math.fsum(v[1:])
    elif op == "multiply":
        result = math.prod(v)
    elif op in ("divide", "ratio"):
        if len(v) != 2:
            raise ToolExecutionError(f"{op} requires exactly two values")
        if v[1] == 0:
            raise ToolExecutionError("division by zero")
        result = v[0] / v[1]
    elif op == "mean":
        result = statistics.fmean(v)
    elif op == "median":
        result = float(statistics.median(v))
    elif op == "min":
        result = min(v)
    elif op == "max":
        result = max(v)
    elif op == "stdev":
        if len(v) < 2:
            raise ToolExecutionError("stdev requires at least two values")
        result = statistics.stdev(v)
    elif op == "percent_change":
        if len(v) != 2 or v[0] == 0:
            raise ToolExecutionError("percent_change requires [old, new] with old != 0")
        result = (v[1] - v[0]) / abs(v[0]) * 100.0
    else:  # pragma: no cover - Literal guards this
        raise ToolExecutionError(f"unsupported operation {op}")
    if not math.isfinite(result):
        raise ToolExecutionError("result is not finite")
    return round(result, 10)


class CalculatorTool(Tool):
    name = "calculator"
    description = (
        "Deterministic arithmetic and statistics. Use for every numerical comparison instead of mental math. "
        "Operations: add, subtract, multiply, divide, sum, mean, median, min, max, stdev, percent_change, ratio, "
        "or evaluate an arithmetic expression."
    )
    args_model = CalculatorArgs
    result_model = CalculatorResult
    timeout_seconds = 5.0

    async def run(self, args: CalculatorArgs, ctx: ToolContext) -> dict[str, Any]:  # type: ignore[override]
        return CalculatorResult(operation=args.operation, result=compute(args), label=args.label).model_dump()
