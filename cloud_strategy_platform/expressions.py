"""Small data-only expression language for deterministic strategy predicates."""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Mapping
from typing import Any, Final, cast

from cloud_strategy_platform.contracts import Scalar


class ExpressionRejectedError(ValueError):
    pass


class ExpressionEvaluationError(ValueError):
    pass


_BINARY: Final = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_COMPARE: Final = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ALLOWED = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    *tuple(_BINARY),
    ast.Compare,
    *tuple(_COMPARE),
    ast.Name,
    ast.Load,
    ast.Constant,
)


class SafeExpression:
    MAX_LENGTH = 4096
    MAX_NODES = 128

    def __init__(self, source: str):
        if not source.strip() or len(source) > self.MAX_LENGTH:
            raise ExpressionRejectedError("expression length is invalid")
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionRejectedError("expression syntax is invalid") from exc
        nodes = tuple(ast.walk(tree))
        if len(nodes) > self.MAX_NODES or any(not isinstance(node, _ALLOWED) for node in nodes):
            raise ExpressionRejectedError("expression contains non-data syntax")
        for node in nodes:
            if isinstance(node, ast.Constant) and not isinstance(
                node.value, (bool, int, float, str)
            ):
                raise ExpressionRejectedError("expression constant type is not allowed")
            if isinstance(node, ast.Name) and node.id.startswith("_"):
                raise ExpressionRejectedError("private names are not allowed")
        self.source = source
        self._tree = tree

    def evaluate(self, context: Mapping[str, Scalar | None]) -> bool:
        try:
            value = self._evaluate(self._tree.body, context)
        except (ArithmeticError, TypeError, ValueError) as exc:
            if isinstance(exc, ExpressionEvaluationError):
                raise
            raise ExpressionEvaluationError("expression evaluation failed") from exc
        if not isinstance(value, bool):
            raise ExpressionEvaluationError("strategy expression must return a boolean")
        return value

    def _evaluate(self, node: ast.expr, context: Mapping[str, Scalar | None]) -> object:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in context or context[node.id] is None:
                raise ExpressionEvaluationError(f"required fact is unavailable: {node.id}")
            return context[node.id]
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(self._evaluate(value, context)) for value in node.values)
            return any(bool(self._evaluate(value, context)) for value in node.values)
        if isinstance(node, ast.UnaryOp):
            operand = self._evaluate(node.operand, context)
            if isinstance(node.op, ast.Not):
                return not bool(operand)
            if not isinstance(operand, (int, float)) or isinstance(operand, bool):
                raise ExpressionEvaluationError("numeric unary operator requires a number")
            return -operand if isinstance(node.op, ast.USub) else +operand
        if isinstance(node, ast.BinOp):
            left = self._evaluate(node.left, context)
            right = self._evaluate(node.right, context)
            operation = _BINARY.get(type(node.op))
            if operation is None:
                raise ExpressionEvaluationError("binary operator is unavailable")
            result = operation(left, right)
            if isinstance(result, float) and not math.isfinite(result):
                raise ExpressionEvaluationError("expression produced a non-finite value")
            return result
        if isinstance(node, ast.Compare):
            left = self._evaluate(node.left, context)
            for operation_node, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._evaluate(comparator, context)
                operation = _COMPARE.get(type(operation_node))
                if operation is None or not operation(cast(Any, left), cast(Any, right)):
                    return False
                left = right
            return True
        raise ExpressionEvaluationError("expression node is unavailable")
