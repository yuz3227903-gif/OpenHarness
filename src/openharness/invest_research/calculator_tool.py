"""Restricted arithmetic tool for transparent investment-research calculations."""

from __future__ import annotations

import ast
import json
import operator
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


MAX_ABS_RESULT = Decimal("1e24")
MAX_EXPRESSION_LENGTH = 500


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_LENGTH)
    variables: dict[str, float | int | str] = Field(default_factory=dict, max_length=50)
    precision: int = Field(default=6, ge=0, le=12)


class CalculatorTool(BaseTool):
    name = "calculator"
    description = (
        "Evaluate one transparent arithmetic expression with optional numeric variables. "
        "Supports +, -, *, /, //, %, ** and parentheses only; no functions or code execution."
    )
    input_model = CalculatorInput

    async def execute(
        self,
        arguments: CalculatorInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        try:
            variables = {
                name: _to_decimal(value)
                for name, value in arguments.variables.items()
                if _valid_variable_name(name)
            }
            if len(variables) != len(arguments.variables):
                raise ValueError("variable names must be valid Python identifiers")
            tree = ast.parse(arguments.expression, mode="eval")
            result = _evaluate(tree.body, variables)
            if not result.is_finite() or abs(result) > MAX_ABS_RESULT:
                raise ValueError("result is outside the permitted numeric range")
            quantum = Decimal(1).scaleb(-arguments.precision)
            rounded = result.quantize(quantum) if arguments.precision else result.quantize(Decimal(1))
        except (SyntaxError, ValueError, InvalidOperation, DivisionByZero, ZeroDivisionError) as exc:
            return ToolResult(
                output=f"calculator failed: {exc}",
                is_error=True,
                metadata={"reason": "invalid_expression"},
            )

        payload = {
            "expression": arguments.expression,
            "variables": {key: str(value) for key, value in variables.items()},
            "result": format(rounded, "f"),
            "precision": arguments.precision,
        }
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False),
            metadata={"operation": "safe_arithmetic"},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        del arguments
        return True


def _valid_variable_name(name: str) -> bool:
    return name.isidentifier() and not name.startswith("_")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid calculator variables")
    return Decimal(str(value))


def _evaluate(node: ast.AST, variables: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numeric constants are allowed")
        return Decimal(str(node.value))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ValueError(f"unknown variable: {node.id}")
        return variables[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand, variables)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, variables)
        right = _evaluate(node.right, variables)
        if isinstance(node.op, ast.Pow):
            if right != right.to_integral_value() or abs(right) > 12:
                raise ValueError("power exponent must be an integer between -12 and 12")
            return left ** int(right)
        operations = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
        }
        operation = operations.get(type(node.op))
        if operation is None:
            raise ValueError(f"operator is not allowed: {type(node.op).__name__}")
        return operation(left, right)
    raise ValueError(f"expression element is not allowed: {type(node).__name__}")


__all__ = ["CalculatorInput", "CalculatorTool"]
