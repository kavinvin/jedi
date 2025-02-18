"""
The :mod:`jedi.api.classes` module contains the return classes of the API.
These classes are the much bigger part of the whole API, because they contain
the interesting information about completion and goto operations.
"""
import re
import warnings

from parso.python.tree import search_ancestor

from jedi import settings
from jedi.evaluate.utils import unite
from jedi.cache import memoize_method
from jedi.evaluate import imports
from jedi.evaluate import compiled
from jedi.evaluate.imports import ImportName
from jedi.evaluate.context import FunctionExecutionContext
from jedi.evaluate.gradual.typeshed import StubModuleContext
from jedi.evaluate.gradual.conversion import name_to_stub, \
    stub_to_actual_context_set, try_stubs_to_actual_context_set
from jedi.api.keywords import KeywordName


def _sort_names_by_start_pos(names):
    return sorted(names, key=lambda s: s.start_pos or (0, 0))


def defined_names(evaluator, context):
    """
    List sub-definitions (e.g., methods in class).

    :type scope: Scope
    :rtype: list of Definition
    """
    filter = next(context.get_filters(search_global=True))
    names = [name for name in filter.values()]
    return [Definition(evaluator, n) for n in _sort_names_by_start_pos(names)]


class BaseDefinition(object):
    _mapping = {
        'posixpath': 'os.path',
        'riscospath': 'os.path',
        'ntpath': 'os.path',
        'os2emxpath': 'os.path',
        'macpath': 'os.path',
        'genericpath': 'os.path',
        'posix': 'os',
        '_io': 'io',
        '_functools': 'functools',
        '_sqlite3': 'sqlite3',
        '__builtin__': 'builtins',
    }

    _tuple_mapping = dict((tuple(k.split('.')), v) for (k, v) in {
        'argparse._ActionsContainer': 'argparse.ArgumentParser',
    }.items())

    def __init__(self, evaluator, name):
        self._evaluator = evaluator
        self._name = name
        """
        An instance of :class:`parso.python.tree.Name` subclass.
        """
        self.is_keyword = isinstance(self._name, KeywordName)

    @memoize_method
    def _get_module(self):
        # This can take a while to complete, because in the worst case of
        # imports (consider `import a` completions), we need to load all
        # modules starting with a first.
        return self._name.get_root_context()

    @property
    def module_path(self):
        """Shows the file path of a module. e.g. ``/usr/lib/python2.7/os.py``"""
        try:
            py__file__ = self._get_module().py__file__
        except AttributeError:
            return None
        else:
            return py__file__()

    @property
    def name(self):
        """
        Name of variable/function/class/module.

        For example, for ``x = None`` it returns ``'x'``.

        :rtype: str or None
        """
        return self._name.string_name

    @property
    def type(self):
        """
        The type of the definition.

        Here is an example of the value of this attribute.  Let's consider
        the following source.  As what is in ``variable`` is unambiguous
        to Jedi, :meth:`jedi.Script.goto_definitions` should return a list of
        definition for ``sys``, ``f``, ``C`` and ``x``.

        >>> from jedi._compatibility import no_unicode_pprint
        >>> from jedi import Script
        >>> source = '''
        ... import keyword
        ...
        ... class C:
        ...     pass
        ...
        ... class D:
        ...     pass
        ...
        ... x = D()
        ...
        ... def f():
        ...     pass
        ...
        ... for variable in [keyword, f, C, x]:
        ...     variable'''

        >>> script = Script(source)
        >>> defs = script.goto_definitions()

        Before showing what is in ``defs``, let's sort it by :attr:`line`
        so that it is easy to relate the result to the source code.

        >>> defs = sorted(defs, key=lambda d: d.line)
        >>> no_unicode_pprint(defs)  # doctest: +NORMALIZE_WHITESPACE
        [<Definition full_name='keyword', description='module keyword'>,
         <Definition full_name='__main__.C', description='class C'>,
         <Definition full_name='__main__.D', description='instance D'>,
         <Definition full_name='__main__.f', description='def f'>]

        Finally, here is what you can get from :attr:`type`:

        >>> defs = [str(d.type) for d in defs]  # It's unicode and in Py2 has u before it.
        >>> defs[0]
        'module'
        >>> defs[1]
        'class'
        >>> defs[2]
        'instance'
        >>> defs[3]
        'function'

        """
        tree_name = self._name.tree_name
        resolve = False
        if tree_name is not None:
            # TODO move this to their respective names.
            definition = tree_name.get_definition()
            if definition is not None and definition.type == 'import_from' and \
                    tree_name.is_definition():
                resolve = True

        if isinstance(self._name, imports.SubModuleName) or resolve:
            for context in self._name.infer():
                return context.api_type
        return self._name.api_type

    @property
    def module_name(self):
        """
        The module name.

        >>> from jedi import Script
        >>> source = 'import json'
        >>> script = Script(source, path='example.py')
        >>> d = script.goto_definitions()[0]
        >>> print(d.module_name)  # doctest: +ELLIPSIS
        json
        """
        return self._get_module().name.string_name

    def in_builtin_module(self):
        """Whether this is a builtin module."""
        if isinstance(self._get_module(), StubModuleContext):
            return any(isinstance(context, compiled.CompiledObject)
                       for context in self._get_module().non_stub_context_set)
        return isinstance(self._get_module(), compiled.CompiledObject)

    @property
    def line(self):
        """The line where the definition occurs (starting with 1)."""
        start_pos = self._name.start_pos
        if start_pos is None:
            return None
        return start_pos[0]

    @property
    def column(self):
        """The column where the definition occurs (starting with 0)."""
        start_pos = self._name.start_pos
        if start_pos is None:
            return None
        return start_pos[1]

    def docstring(self, raw=False, fast=True):
        r"""
        Return a document string for this completion object.

        Example:

        >>> from jedi import Script
        >>> source = '''\
        ... def f(a, b=1):
        ...     "Document for function f."
        ... '''
        >>> script = Script(source, 1, len('def f'), 'example.py')
        >>> doc = script.goto_definitions()[0].docstring()
        >>> print(doc)
        f(a, b=1)
        <BLANKLINE>
        Document for function f.

        Notice that useful extra information is added to the actual
        docstring.  For function, it is call signature.  If you need
        actual docstring, use ``raw=True`` instead.

        >>> print(script.goto_definitions()[0].docstring(raw=True))
        Document for function f.

        :param fast: Don't follow imports that are only one level deep like
            ``import foo``, but follow ``from foo import bar``. This makes
            sense for speed reasons. Completing `import a` is slow if you use
            the ``foo.docstring(fast=False)`` on every object, because it
            parses all libraries starting with ``a``.
        """
        return _Help(self._name).docstring(fast=fast, raw=raw)

    @property
    def description(self):
        """A textual description of the object."""
        return self._name.string_name

    @property
    def full_name(self):
        """
        Dot-separated path of this object.

        It is in the form of ``<module>[.<submodule>[...]][.<object>]``.
        It is useful when you want to look up Python manual of the
        object at hand.

        Example:

        >>> from jedi import Script
        >>> source = '''
        ... import os
        ... os.path.join'''
        >>> script = Script(source, 3, len('os.path.join'), 'example.py')
        >>> print(script.goto_definitions()[0].full_name)
        os.path.join

        Notice that it returns ``'os.path.join'`` instead of (for example)
        ``'posixpath.join'``. This is not correct, since the modules name would
        be ``<module 'posixpath' ...>```. However most users find the latter
        more practical.
        """
        if not self._name.is_context_name:
            return None

        names = self._name.get_qualified_names(include_module_names=True)
        if names is None:
            return names

        names = list(names)
        try:
            names[0] = self._mapping[names[0]]
        except KeyError:
            pass

        return '.'.join(names)

    def is_stub(self):
        if not self._name.is_context_name:
            return False
        return all(c.is_stub() for c in self._name.infer())

    def goto_stubs(self):
        if not self._name.is_context_name:
            return []

        if self.is_stub():
            return [self]

        return [
            Definition(self._evaluator, stub_def.name)
            for stub_def in name_to_stub(self._name)
        ]

    def goto_assignments(self):
        if not self._name.is_context_name:
            return []

        return [self if n == self._name else Definition(self._evaluator, n)
                for n in self._name.goto()]

    def infer(self):
        if not self._name.is_context_name:
            return []

        # Param names are special because they are not handled by
        # the evaluator method.
        context_set = try_stubs_to_actual_context_set(
            self._name.infer(),
            prefer_stub_to_compiled=True,
        )
        return [Definition(self._evaluator, d.name) for d in context_set]

    @property
    @memoize_method
    def params(self):
        """
        Raises an ``AttributeError``if the definition is not callable.
        Otherwise returns a list of `Definition` that represents the params.
        """
        # Only return the first one. There might be multiple one, especially
        # with overloading.
        for context in self._name.infer():
            for signature in context.get_signatures():
                return [Definition(self._evaluator, n) for n in signature.get_param_names()]

        if self.type == 'function' or self.type == 'class':
            # Fallback, if no signatures were defined (which is probably by
            # itself a bug).
            return []
        raise AttributeError('There are no params defined on this.')

    def parent(self):
        if not self._name.is_context_name:
            return None

        context = self._name.parent_context
        if context is None:
            return None

        if isinstance(context, FunctionExecutionContext):
            context = context.function_context
        return Definition(self._evaluator, context.name)

    def __repr__(self):
        return "<%s full_name=%r, description=%r>" % (
            self.__class__.__name__,
            self.full_name,
            self.description,
        )

    def get_line_code(self, before=0, after=0):
        """
        Returns the line of code where this object was defined.

        :param before: Add n lines before the current line to the output.
        :param after: Add n lines after the current line to the output.

        :return str: Returns the line(s) of code or an empty string if it's a
                     builtin.
        """
        if not self._name.is_context_name or self.in_builtin_module():
            return ''

        lines = self._name.get_root_context().code_lines

        index = self._name.start_pos[0] - 1
        start_index = max(index - before, 0)
        return ''.join(lines[start_index:index + after + 1])


class Completion(BaseDefinition):
    """
    `Completion` objects are returned from :meth:`api.Script.completions`. They
    provide additional information about a completion.
    """
    def __init__(self, evaluator, name, stack, like_name_length):
        super(Completion, self).__init__(evaluator, name)

        self._like_name_length = like_name_length
        self._stack = stack

        # Completion objects with the same Completion name (which means
        # duplicate items in the completion)
        self._same_name_completions = []

    def _complete(self, like_name):
        append = ''
        if settings.add_bracket_after_function \
                and self.type == 'function':
            append = '('

        if self._name.api_type == 'param' and self._stack is not None:
            nonterminals = [stack_node.nonterminal for stack_node in self._stack]
            if 'trailer' in nonterminals and 'argument' not in nonterminals:
                # TODO this doesn't work for nested calls.
                append += '='

        name = self._name.string_name
        if like_name:
            name = name[self._like_name_length:]
        return name + append

    @property
    def complete(self):
        """
        Return the rest of the word, e.g. completing ``isinstance``::

            isinstan# <-- Cursor is here

        would return the string 'ce'. It also adds additional stuff, depending
        on your `settings.py`.

        Assuming the following function definition::

            def foo(param=0):
                pass

        completing ``foo(par`` would give a ``Completion`` which `complete`
        would be `am=`


        """
        return self._complete(True)

    @property
    def name_with_symbols(self):
        """
        Similar to :attr:`name`, but like :attr:`name` returns also the
        symbols, for example assuming the following function definition::

            def foo(param=0):
                pass

        completing ``foo(`` would give a ``Completion`` which
        ``name_with_symbols`` would be "param=".

        """
        return self._complete(False)

    def docstring(self, raw=False, fast=True):
        if self._like_name_length >= 3:
            # In this case we can just resolve the like name, because we
            # wouldn't load like > 100 Python modules anymore.
            fast = False
        return super(Completion, self).docstring(raw=raw, fast=fast)

    @property
    def description(self):
        """Provide a description of the completion object."""
        # TODO improve the class structure.
        return Definition.description.__get__(self)

    def __repr__(self):
        return '<%s: %s>' % (type(self).__name__, self._name.string_name)

    @memoize_method
    def follow_definition(self):
        """
        Deprecated!

        Return the original definitions. I strongly recommend not using it for
        your completions, because it might slow down |jedi|. If you want to
        read only a few objects (<=20), it might be useful, especially to get
        the original docstrings. The basic problem of this function is that it
        follows all results. This means with 1000 completions (e.g.  numpy),
        it's just PITA-slow.
        """
        warnings.warn(
            "Deprecated since version 0.14.0. Use .infer.",
            DeprecationWarning,
            stacklevel=2
        )
        return self.infer()


class Definition(BaseDefinition):
    """
    *Definition* objects are returned from :meth:`api.Script.goto_assignments`
    or :meth:`api.Script.goto_definitions`.
    """
    def __init__(self, evaluator, definition):
        super(Definition, self).__init__(evaluator, definition)

    @property
    def description(self):
        """
        A description of the :class:`.Definition` object, which is heavily used
        in testing. e.g. for ``isinstance`` it returns ``def isinstance``.

        Example:

        >>> from jedi._compatibility import no_unicode_pprint
        >>> from jedi import Script
        >>> source = '''
        ... def f():
        ...     pass
        ...
        ... class C:
        ...     pass
        ...
        ... variable = f if random.choice([0,1]) else C'''
        >>> script = Script(source, column=3)  # line is maximum by default
        >>> defs = script.goto_definitions()
        >>> defs = sorted(defs, key=lambda d: d.line)
        >>> no_unicode_pprint(defs)  # doctest: +NORMALIZE_WHITESPACE
        [<Definition full_name='__main__.f', description='def f'>,
         <Definition full_name='__main__.C', description='class C'>]
        >>> str(defs[0].description)  # strip literals in python2
        'def f'
        >>> str(defs[1].description)
        'class C'

        """
        typ = self.type
        tree_name = self._name.tree_name
        if typ in ('function', 'class', 'module', 'instance') or tree_name is None:
            if typ == 'function':
                # For the description we want a short and a pythonic way.
                typ = 'def'
            return typ + ' ' + self._name.string_name
        elif typ == 'param':
            code = search_ancestor(tree_name, 'param').get_code(
                include_prefix=False,
                include_comma=False
            )
            return typ + ' ' + code

        definition = tree_name.get_definition() or tree_name
        # Remove the prefix, because that's not what we want for get_code
        # here.
        txt = definition.get_code(include_prefix=False)
        # Delete comments:
        txt = re.sub(r'#[^\n]+\n', ' ', txt)
        # Delete multi spaces/newlines
        txt = re.sub(r'\s+', ' ', txt).strip()
        return txt

    @property
    def desc_with_module(self):
        """
        In addition to the definition, also return the module.

        .. warning:: Don't use this function yet, its behaviour may change. If
            you really need it, talk to me.

        .. todo:: Add full path. This function is should return a
            `module.class.function` path.
        """
        position = '' if self.in_builtin_module else '@%s' % self.line
        return "%s:%s%s" % (self.module_name, self.description, position)

    @memoize_method
    def defined_names(self):
        """
        List sub-definitions (e.g., methods in class).

        :rtype: list of Definition
        """
        defs = self._name.infer()
        return sorted(
            unite(defined_names(self._evaluator, d) for d in defs),
            key=lambda s: s._name.start_pos or (0, 0)
        )

    def is_definition(self):
        """
        Returns True, if defined as a name in a statement, function or class.
        Returns False, if it's a reference to such a definition.
        """
        if self._name.tree_name is None:
            return True
        else:
            return self._name.tree_name.is_definition()

    def __eq__(self, other):
        return self._name.start_pos == other._name.start_pos \
            and self.module_path == other.module_path \
            and self.name == other.name \
            and self._evaluator == other._evaluator

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self._name.start_pos, self.module_path, self.name, self._evaluator))


class CallSignature(Definition):
    """
    `CallSignature` objects is the return value of `Script.function_definition`.
    It knows what functions you are currently in. e.g. `isinstance(` would
    return the `isinstance` function. without `(` it would return nothing.
    """
    def __init__(self, evaluator, signature, bracket_start_pos, index, key_name_str):
        super(CallSignature, self).__init__(evaluator, signature.name)
        self._index = index
        self._key_name_str = key_name_str
        self._bracket_start_pos = bracket_start_pos
        self._signature = signature

    @property
    def index(self):
        """
        The Param index of the current call.
        Returns None if the index cannot be found in the curent call.
        """
        if self._key_name_str is not None:
            for i, param in enumerate(self.params):
                if self._key_name_str == param.name:
                    return i
            if self.params:
                param_name = self.params[-1]._name
                if param_name.tree_name is not None:
                    if param_name.tree_name.get_definition().star_count == 2:
                        return i
            return None

        if self._index >= len(self.params):
            for i, param in enumerate(self.params):
                tree_name = param._name.tree_name
                if tree_name is not None:
                    # *args case
                    if tree_name.get_definition().star_count == 1:
                        return i
            return None
        return self._index

    @property
    def params(self):
        return [Definition(self._evaluator, n) for n in self._signature.get_param_names()]

    @property
    def bracket_start(self):
        """
        The indent of the bracket that is responsible for the last function
        call.
        """
        return self._bracket_start_pos

    @property
    def _params_str(self):
        return ', '.join([p.description[6:]
                          for p in self.params])

    def __repr__(self):
        return '<%s: %s index=%r params=[%s]>' % (
            type(self).__name__,
            self._name.string_name,
            self._index,
            self._params_str,
        )


def _format_signatures(context):
    return '\n'.join(
        signature.to_string()
        for signature in context.get_signatures()
    )


class _Help(object):
    """
    Temporary implementation, will be used as `Script.help() or something in
    the future.
    """
    def __init__(self, definition):
        self._name = definition

    @memoize_method
    def _get_contexts(self, fast):
        if isinstance(self._name, ImportName) and fast:
            return {}

        if self._name.api_type == 'statement':
            return {}

        return self._name.infer()

    def docstring(self, fast=True, raw=True):
        """
        The docstring ``__doc__`` for any object.

        See :attr:`doc` for example.
        """
        full_doc = ''
        # Using the first docstring that we see.
        for context in self._get_contexts(fast=fast):
            if full_doc:
                # In case we have multiple contexts, just return all of them
                # separated by a few dashes.
                full_doc += '\n' + '-' * 30 + '\n'

            doc = context.py__doc__()

            signature_text = ''
            if self._name.is_context_name:
                if not raw:
                    signature_text = _format_signatures(context)
                if not doc and context.is_stub():
                    for c in stub_to_actual_context_set(context):
                        doc = c.py__doc__()
                        if doc:
                            break

            if signature_text and doc:
                full_doc += signature_text + '\n\n' + doc
            else:
                full_doc += signature_text + doc

        return full_doc
