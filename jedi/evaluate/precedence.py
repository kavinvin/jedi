"""
Handles operator precedence.
"""
import operator as op

from jedi._compatibility import unicode
from jedi.evaluate.compiled import CompiledObject, create, builtin_from_name
from jedi.evaluate import analysis
from jedi.evaluate.context import ContextSet, NO_CONTEXTS, iterator_to_context_set

# Maps Python syntax to the operator module.
COMPARISON_OPERATORS = {
    '==': op.eq,
    '!=': op.ne,
    'is': op.is_,
    'is not': op.is_not,
    '<': op.lt,
    '<=': op.le,
    '>': op.gt,
    '>=': op.ge,
}


def literals_to_types(evaluator, result):
    # Changes literals ('a', 1, 1.0, etc) to its type instances (str(),
    # int(), float(), etc).
    new_result = NO_CONTEXTS
    for typ in result:
        if is_literal(typ):
            # Literals are only valid as long as the operations are
            # correct. Otherwise add a value-free instance.
            cls = builtin_from_name(evaluator, typ.name.string_name)
            new_result |= cls.execute_evaluated()
        else:
            new_result |= ContextSet(typ)
    return new_result


def calculate(evaluator, context, left_contexts, operator, right_contexts):
    if not left_contexts or not right_contexts:
        # illegal slices e.g. cause left/right_result to be None
        result = (left_contexts or NO_CONTEXTS) | (right_contexts or NO_CONTEXTS)
        return literals_to_types(evaluator, result)
    else:
        # I don't think there's a reasonable chance that a string
        # operation is still correct, once we pass something like six
        # objects.
        if len(left_contexts) * len(right_contexts) > 6:
            return literals_to_types(evaluator, left_contexts | right_contexts)
        else:
            return ContextSet.from_sets(
                _element_calculate(evaluator, context, left, operator, right)
                for left in left_contexts
                for right in right_contexts
            )


@iterator_to_context_set
def factor_calculate(evaluator, types, operator):
    """
    Calculates `+`, `-`, `~` and `not` prefixes.
    """
    for typ in types:
        if operator == '-':
            if _is_number(typ):
                yield create(evaluator, -typ.obj)
        elif operator == 'not':
            value = typ.py__bool__()
            if value is None:  # Uncertainty.
                return
            yield create(evaluator, not value)
        else:
            yield typ


def _is_number(obj):
    return isinstance(obj, CompiledObject) \
        and isinstance(obj.obj, (int, float))


def is_string(obj):
    return isinstance(obj, CompiledObject) \
        and isinstance(obj.obj, (str, unicode))


def is_literal(obj):
    return _is_number(obj) or is_string(obj)


def _is_tuple(obj):
    from jedi.evaluate import iterable
    return isinstance(obj, iterable.AbstractSequence) and obj.array_type == 'tuple'


def _is_list(obj):
    from jedi.evaluate import iterable
    return isinstance(obj, iterable.AbstractSequence) and obj.array_type == 'list'


def _element_calculate(evaluator, context, left, operator, right):
    from jedi.evaluate import iterable, instance
    l_is_num = _is_number(left)
    r_is_num = _is_number(right)
    if operator == '*':
        # for iterables, ignore * operations
        if isinstance(left, iterable.AbstractSequence) or is_string(left):
            return ContextSet(left)
        elif isinstance(right, iterable.AbstractSequence) or is_string(right):
            return ContextSet(right)
    elif operator == '+':
        if l_is_num and r_is_num or is_string(left) and is_string(right):
            return ContextSet(create(evaluator, left.obj + right.obj))
        elif _is_tuple(left) and _is_tuple(right) or _is_list(left) and _is_list(right):
            return ContextSet(iterable.MergedArray(evaluator, (left, right)))
    elif operator == '-':
        if l_is_num and r_is_num:
            return ContextSet(create(evaluator, left.obj - right.obj))
    elif operator == '%':
        # With strings and numbers the left type typically remains. Except for
        # `int() % float()`.
        return ContextSet(left)
    elif operator in COMPARISON_OPERATORS:
        operation = COMPARISON_OPERATORS[operator]
        if isinstance(left, CompiledObject) and isinstance(right, CompiledObject):
            # Possible, because the return is not an option. Just compare.
            left = left.obj
            right = right.obj

        try:
            result = operation(left, right)
        except TypeError:
            # Could be True or False.
            return ContextSet(create(evaluator, True), create(evaluator, False))
        else:
            return ContextSet(create(evaluator, result))
    elif operator == 'in':
        return NO_CONTEXTS

    def check(obj):
        """Checks if a Jedi object is either a float or an int."""
        return isinstance(obj, instance.CompiledInstance) and \
            obj.name.string_name in ('int', 'float')

    # Static analysis, one is a number, the other one is not.
    if operator in ('+', '-') and l_is_num != r_is_num \
            and not (check(left) or check(right)):
        message = "TypeError: unsupported operand type(s) for +: %s and %s"
        analysis.add(context, 'type-error-operation', operator,
                     message % (left, right))

    return ContextSet(left, right)
