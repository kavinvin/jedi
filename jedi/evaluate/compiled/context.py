"""
Imitate the parser representation.
"""
import re
from functools import partial

from jedi import debug
from jedi.evaluate.utils import to_list
from jedi._compatibility import force_unicode, Parameter, cast_path
from jedi.cache import underscore_memoization, memoize_method
from jedi.evaluate.filters import AbstractFilter
from jedi.evaluate.names import AbstractNameDefinition, ContextNameMixin, \
    ParamNameInterface
from jedi.evaluate.base_context import Context, ContextSet, NO_CONTEXTS
from jedi.evaluate.lazy_context import LazyKnownContext
from jedi.evaluate.compiled.access import _sentinel
from jedi.evaluate.cache import evaluator_function_cache
from jedi.evaluate.helpers import reraise_getitem_errors, execute_evaluated
from jedi.evaluate.signature import BuiltinSignature


class CheckAttribute(object):
    """Raises an AttributeError if the attribute X isn't available."""
    def __init__(self, check_name=None):
        # Remove the py in front of e.g. py__call__.
        self.check_name = check_name

    def __call__(self, func):
        self.func = func
        if self.check_name is None:
            self.check_name = force_unicode(func.__name__[2:])
        return self

    def __get__(self, instance, owner):
        if instance is None:
            return self

        # This might raise an AttributeError. That's wanted.
        instance.access_handle.getattr_paths(self.check_name)
        return partial(self.func, instance)


class CompiledObject(Context):
    def __init__(self, evaluator, access_handle, parent_context=None):
        super(CompiledObject, self).__init__(evaluator, parent_context)
        self.access_handle = access_handle

    def py__call__(self, arguments):
        try:
            self.access_handle.getattr_paths(u'__call__')
        except AttributeError:
            return super(CompiledObject, self).py__call__(arguments)
        else:
            if self.access_handle.is_class():
                from jedi.evaluate.context import CompiledInstance
                return ContextSet([
                    CompiledInstance(self.evaluator, self.parent_context, self, arguments)
                ])
            else:
                return ContextSet(self._execute_function(arguments))

    @CheckAttribute()
    def py__class__(self):
        return create_from_access_path(self.evaluator, self.access_handle.py__class__())

    @CheckAttribute()
    def py__mro__(self):
        return (self,) + tuple(
            create_from_access_path(self.evaluator, access)
            for access in self.access_handle.py__mro__accesses()
        )

    @CheckAttribute()
    def py__bases__(self):
        return tuple(
            create_from_access_path(self.evaluator, access)
            for access in self.access_handle.py__bases__()
        )

    @CheckAttribute()
    def py__path__(self):
        return map(cast_path, self.access_handle.py__path__())

    @property
    def string_names(self):
        # For modules
        name = self.py__name__()
        if name is None:
            return ()
        return tuple(name.split('.'))

    def get_qualified_names(self):
        return self.access_handle.get_qualified_names()

    def py__bool__(self):
        return self.access_handle.py__bool__()

    def py__file__(self):
        return cast_path(self.access_handle.py__file__())

    def is_class(self):
        return self.access_handle.is_class()

    def is_module(self):
        return self.access_handle.is_module()

    def is_compiled(self):
        return True

    def is_stub(self):
        return False

    def is_instance(self):
        return self.access_handle.is_instance()

    def py__doc__(self):
        return self.access_handle.py__doc__()

    @to_list
    def get_param_names(self):
        try:
            signature_params = self.access_handle.get_signature_params()
        except ValueError:  # Has no signature
            params_str, ret = self._parse_function_doc()
            tokens = params_str.split(',')
            if self.access_handle.ismethoddescriptor():
                tokens.insert(0, 'self')
            for p in tokens:
                name, _, default = p.strip().partition('=')
                yield UnresolvableParamName(self, name, default)
        else:
            for signature_param in signature_params:
                yield SignatureParamName(self, signature_param)

    def get_signatures(self):
        _, return_string = self._parse_function_doc()
        return [BuiltinSignature(self, return_string)]

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.access_handle.get_repr())

    @underscore_memoization
    def _parse_function_doc(self):
        doc = self.py__doc__()
        if doc is None:
            return '', ''

        return _parse_function_doc(doc)

    @property
    def api_type(self):
        return self.access_handle.get_api_type()

    @underscore_memoization
    def _cls(self):
        """
        We used to limit the lookups for instantiated objects like list(), but
        this is not the case anymore. Python itself
        """
        # Ensures that a CompiledObject is returned that is not an instance (like list)
        return self

    def get_filters(self, search_global=False, is_instance=False,
                    until_position=None, origin_scope=None):
        yield self._ensure_one_filter(is_instance)

    @memoize_method
    def _ensure_one_filter(self, is_instance):
        """
        search_global shouldn't change the fact that there's one dict, this way
        there's only one `object`.
        """
        return CompiledObjectFilter(self.evaluator, self, is_instance)

    @CheckAttribute(u'__getitem__')
    def py__simple_getitem__(self, index):
        with reraise_getitem_errors(IndexError, KeyError, TypeError):
            access = self.access_handle.py__simple_getitem__(index)
        if access is None:
            return NO_CONTEXTS

        return ContextSet([create_from_access_path(self.evaluator, access)])

    def py__getitem__(self, index_context_set, contextualized_node):
        all_access_paths = self.access_handle.py__getitem__all_values()
        if all_access_paths is None:
            # This means basically that no __getitem__ has been defined on this
            # object.
            return super(CompiledObject, self).py__getitem__(index_context_set, contextualized_node)
        return ContextSet(
            create_from_access_path(self.evaluator, access)
            for access in all_access_paths
        )

    def py__iter__(self, contextualized_node=None):
        # Python iterators are a bit strange, because there's no need for
        # the __iter__ function as long as __getitem__ is defined (it will
        # just start with __getitem__(0). This is especially true for
        # Python 2 strings, where `str.__iter__` is not even defined.
        if not self.access_handle.has_iter():
            for x in super(CompiledObject, self).py__iter__(contextualized_node):
                yield x

        access_path_list = self.access_handle.py__iter__list()
        if access_path_list is None:
            # There is no __iter__ method on this object.
            return

        for access in access_path_list:
            yield LazyKnownContext(create_from_access_path(self.evaluator, access))

    def py__name__(self):
        return self.access_handle.py__name__()

    @property
    def name(self):
        name = self.py__name__()
        if name is None:
            name = self.access_handle.get_repr()
        return CompiledContextName(self, name)

    def _execute_function(self, params):
        from jedi.evaluate import docstrings
        from jedi.evaluate.compiled import builtin_from_name
        if self.api_type != 'function':
            return

        for name in self._parse_function_doc()[1].split():
            try:
                # TODO wtf is this? this is exactly the same as the thing
                # below. It uses getattr as well.
                self.evaluator.builtins_module.access_handle.getattr_paths(name)
            except AttributeError:
                continue
            else:
                bltn_obj = builtin_from_name(self.evaluator, name)
                for result in self.evaluator.execute(bltn_obj, params):
                    yield result
        for type_ in docstrings.infer_return_types(self):
            yield type_

    def get_safe_value(self, default=_sentinel):
        try:
            return self.access_handle.get_safe_value()
        except ValueError:
            if default == _sentinel:
                raise
            return default

    def execute_operation(self, other, operator):
        return create_from_access_path(
            self.evaluator,
            self.access_handle.execute_operation(other.access_handle, operator)
        )

    def negate(self):
        return create_from_access_path(self.evaluator, self.access_handle.negate())


class CompiledName(AbstractNameDefinition):
    def __init__(self, evaluator, parent_context, name):
        self._evaluator = evaluator
        self.parent_context = parent_context
        self.string_name = name

    def __repr__(self):
        try:
            name = self.parent_context.name  # __name__ is not defined all the time
        except AttributeError:
            name = None
        return '<%s: (%s).%s>' % (self.__class__.__name__, name, self.string_name)

    @property
    def api_type(self):
        return next(iter(self.infer())).api_type

    @underscore_memoization
    def infer(self):
        return ContextSet([_create_from_name(
            self._evaluator, self.parent_context, self.string_name
        )])


class SignatureParamName(AbstractNameDefinition, ParamNameInterface):
    api_type = u'param'

    def __init__(self, compiled_obj, signature_param):
        self.parent_context = compiled_obj.parent_context
        self._signature_param = signature_param

    @property
    def string_name(self):
        return self._signature_param.name

    def to_string(self):
        s = self.string_name
        if self._signature_param.has_annotation:
            s += ': ' + self._signature_param.annotation_string
        if self._signature_param.has_default:
            s += '=' + self._signature_param.default_string
        return s

    def get_kind(self):
        return getattr(Parameter, self._signature_param.kind_name)

    def is_keyword_param(self):
        return self._signature_param

    def infer(self):
        p = self._signature_param
        evaluator = self.parent_context.evaluator
        contexts = NO_CONTEXTS
        if p.has_default:
            contexts = ContextSet([create_from_access_path(evaluator, p.default)])
        if p.has_annotation:
            annotation = create_from_access_path(evaluator, p.annotation)
            contexts |= execute_evaluated(annotation)
        return contexts


class UnresolvableParamName(AbstractNameDefinition, ParamNameInterface):
    api_type = u'param'

    def __init__(self, compiled_obj, name, default):
        self.parent_context = compiled_obj.parent_context
        self.string_name = name
        self._default = default

    def get_kind(self):
        return Parameter.POSITIONAL_ONLY

    def to_string(self):
        string = self.string_name
        if self._default:
            string += '=' + self._default
        return string

    def infer(self):
        return NO_CONTEXTS


class CompiledContextName(ContextNameMixin, AbstractNameDefinition):
    def __init__(self, context, name):
        self.string_name = name
        self._context = context
        self.parent_context = context.parent_context


class EmptyCompiledName(AbstractNameDefinition):
    """
    Accessing some names will raise an exception. To avoid not having any
    completions, just give Jedi the option to return this object. It infers to
    nothing.
    """
    def __init__(self, evaluator, name):
        self.parent_context = evaluator.builtins_module
        self.string_name = name

    def infer(self):
        return NO_CONTEXTS


class CompiledObjectFilter(AbstractFilter):
    name_class = CompiledName

    def __init__(self, evaluator, compiled_object, is_instance=False):
        self._evaluator = evaluator
        self._compiled_object = compiled_object
        self.is_instance = is_instance

    def get(self, name):
        return self._get(
            name,
            lambda: self._compiled_object.access_handle.is_allowed_getattr(name),
            lambda: self._compiled_object.access_handle.dir(),
            check_has_attribute=True
        )

    def _get(self, name, allowed_getattr_callback, dir_callback, check_has_attribute=False):
        """
        To remove quite a few access calls we introduced the callback here.
        """
        has_attribute, is_descriptor = allowed_getattr_callback()
        if check_has_attribute and not has_attribute:
            return []

        # Always use unicode objects in Python 2 from here.
        name = force_unicode(name)

        if is_descriptor or not has_attribute:
            return [self._get_cached_name(name, is_empty=True)]

        if self.is_instance and name not in dir_callback():
            return []
        return [self._get_cached_name(name)]

    @memoize_method
    def _get_cached_name(self, name, is_empty=False):
        if is_empty:
            return EmptyCompiledName(self._evaluator, name)
        else:
            return self._create_name(name)

    def values(self):
        from jedi.evaluate.compiled import builtin_from_name
        names = []
        needs_type_completions, dir_infos = self._compiled_object.access_handle.get_dir_infos()
        for name in dir_infos:
            names += self._get(
                name,
                lambda: dir_infos[name],
                lambda: dir_infos.keys(),
            )

        # ``dir`` doesn't include the type names.
        if not self.is_instance and needs_type_completions:
            for filter in builtin_from_name(self._evaluator, u'type').get_filters():
                names += filter.values()
        return names

    def _create_name(self, name):
        return self.name_class(self._evaluator, self._compiled_object, name)

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self._compiled_object)


docstr_defaults = {
    'floating point number': u'float',
    'character': u'str',
    'integer': u'int',
    'dictionary': u'dict',
    'string': u'str',
}


def _parse_function_doc(doc):
    """
    Takes a function and returns the params and return value as a tuple.
    This is nothing more than a docstring parser.

    TODO docstrings like utime(path, (atime, mtime)) and a(b [, b]) -> None
    TODO docstrings like 'tuple of integers'
    """
    doc = force_unicode(doc)
    # parse round parentheses: def func(a, (b,c))
    try:
        count = 0
        start = doc.index('(')
        for i, s in enumerate(doc[start:]):
            if s == '(':
                count += 1
            elif s == ')':
                count -= 1
            if count == 0:
                end = start + i
                break
        param_str = doc[start + 1:end]
    except (ValueError, UnboundLocalError):
        # ValueError for doc.index
        # UnboundLocalError for undefined end in last line
        debug.dbg('no brackets found - no param')
        end = 0
        param_str = u''
    else:
        # remove square brackets, that show an optional param ( = None)
        def change_options(m):
            args = m.group(1).split(',')
            for i, a in enumerate(args):
                if a and '=' not in a:
                    args[i] += '=None'
            return ','.join(args)

        while True:
            param_str, changes = re.subn(r' ?\[([^\[\]]+)\]',
                                         change_options, param_str)
            if changes == 0:
                break
    param_str = param_str.replace('-', '_')  # see: isinstance.__doc__

    # parse return value
    r = re.search(u'-[>-]* ', doc[end:end + 7])
    if r is None:
        ret = u''
    else:
        index = end + r.end()
        # get result type, which can contain newlines
        pattern = re.compile(r'(,\n|[^\n-])+')
        ret_str = pattern.match(doc, index).group(0).strip()
        # New object -> object()
        ret_str = re.sub(r'[nN]ew (.*)', r'\1()', ret_str)

        ret = docstr_defaults.get(ret_str, ret_str)

    return param_str, ret


def _create_from_name(evaluator, compiled_object, name):
    access_paths = compiled_object.access_handle.getattr_paths(name, default=None)
    parent_context = compiled_object
    if parent_context.is_class():
        parent_context = parent_context.parent_context

    context = None
    for access_path in access_paths:
        context = create_cached_compiled_object(
            evaluator, access_path, parent_context=context
        )
    return context


def _normalize_create_args(func):
    """The cache doesn't care about keyword vs. normal args."""
    def wrapper(evaluator, obj, parent_context=None):
        return func(evaluator, obj, parent_context)
    return wrapper


def create_from_access_path(evaluator, access_path):
    parent_context = None
    for name, access in access_path.accesses:
        parent_context = create_cached_compiled_object(evaluator, access, parent_context)
    return parent_context


@_normalize_create_args
@evaluator_function_cache()
def create_cached_compiled_object(evaluator, access_handle, parent_context):
    return CompiledObject(evaluator, access_handle, parent_context)
