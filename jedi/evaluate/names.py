from abc import abstractmethod

from parso.tree import search_ancestor

from jedi._compatibility import Parameter
from jedi.evaluate.base_context import ContextSet
from jedi.cache import memoize_method


class AbstractNameDefinition(object):
    start_pos = None
    string_name = None
    parent_context = None
    tree_name = None
    is_context_name = True
    """
    Used for the Jedi API to know if it's a keyword or an actual name.
    """

    @abstractmethod
    def infer(self):
        raise NotImplementedError

    @abstractmethod
    def goto(self):
        # Typically names are already definitions and therefore a goto on that
        # name will always result on itself.
        return {self}

    @abstractmethod
    def get_qualified_names(self, include_module_names=False):
        raise NotImplementedError

    def get_root_context(self):
        return self.parent_context.get_root_context()

    def __repr__(self):
        if self.start_pos is None:
            return '<%s: string_name=%s>' % (self.__class__.__name__, self.string_name)
        return '<%s: string_name=%s start_pos=%s>' % (self.__class__.__name__,
                                                      self.string_name, self.start_pos)

    def is_import(self):
        return False

    @property
    def api_type(self):
        return self.parent_context.api_type


class AbstractTreeName(AbstractNameDefinition):
    def __init__(self, parent_context, tree_name):
        self.parent_context = parent_context
        self.tree_name = tree_name

    def get_qualified_names(self, include_module_names=False):
        import_node = search_ancestor(self.tree_name, 'import_name', 'import_from')
        if import_node is not None:
            return tuple(n.value for n in import_node.get_path_for_name(self.tree_name))

        parent_names = self.parent_context.get_qualified_names()
        if parent_names is None:
            return None
        parent_names += (self.tree_name.value,)
        if include_module_names:
            module_names = self.get_root_context().string_names
            if module_names is None:
                return None
            return module_names + parent_names
        return parent_names

    def goto(self):
        return self.parent_context.evaluator.goto(self.parent_context, self.tree_name)

    def is_import(self):
        imp = search_ancestor(self.tree_name, 'import_from', 'import_name')
        return imp is not None

    @property
    def string_name(self):
        return self.tree_name.value

    @property
    def start_pos(self):
        return self.tree_name.start_pos


class ContextNameMixin(object):
    def infer(self):
        return ContextSet([self._context])

    def get_qualified_names(self, include_module_names=False):
        qualified_names = self._context.get_qualified_names()
        if qualified_names is None or not include_module_names:
            return qualified_names

        module_names = self.get_root_context().string_names
        if module_names is None:
            return None
        return module_names + qualified_names

    def get_root_context(self):
        if self.parent_context is None:  # A module
            return self._context
        return super(ContextNameMixin, self).get_root_context()

    @property
    def api_type(self):
        return self._context.api_type


class ContextName(ContextNameMixin, AbstractTreeName):
    def __init__(self, context, tree_name):
        super(ContextName, self).__init__(context.parent_context, tree_name)
        self._context = context

    def goto(self):
        from jedi.evaluate.gradual.conversion import try_stub_to_actual_names
        return try_stub_to_actual_names([self._context.name])


class TreeNameDefinition(AbstractTreeName):
    _API_TYPES = dict(
        import_name='module',
        import_from='module',
        funcdef='function',
        param='param',
        classdef='class',
    )

    def infer(self):
        # Refactor this, should probably be here.
        from jedi.evaluate.syntax_tree import tree_name_to_contexts
        parent = self.parent_context
        return tree_name_to_contexts(parent.evaluator, parent, self.tree_name)

    @property
    def api_type(self):
        definition = self.tree_name.get_definition(import_name_always=True)
        if definition is None:
            return 'statement'
        return self._API_TYPES.get(definition.type, 'statement')


class ParamNameInterface(object):
    def get_kind(self):
        raise NotImplementedError

    def to_string(self):
        raise NotImplementedError


class ParamName(AbstractTreeName, ParamNameInterface):
    api_type = u'param'

    def __init__(self, parent_context, tree_name):
        self.parent_context = parent_context
        self.tree_name = tree_name

    def _get_param_node(self):
        return search_ancestor(self.tree_name, 'param')

    def get_kind(self):
        tree_param = self._get_param_node()
        if tree_param.star_count == 1:  # *args
            return Parameter.VAR_POSITIONAL
        if tree_param.star_count == 2:  # **kwargs
            return Parameter.VAR_KEYWORD

        parent = tree_param.parent
        for p in parent.children:
            if p.type == 'param':
                if p.star_count:
                    return Parameter.KEYWORD_ONLY
                if p == tree_param:
                    break
        return Parameter.POSITIONAL_OR_KEYWORD

    def to_string(self):
        output = self.string_name
        param_node = self._get_param_node()
        if param_node.annotation is not None:
            output += ': ' + param_node.annotation.get_code(include_prefix=False)
        if param_node.default is not None:
            output += '=' + param_node.default.get_code(include_prefix=False)
        return output

    def infer(self):
        return self.get_param().infer()

    def get_param(self):
        params, _ = self.parent_context.get_executed_params_and_issues()
        param_node = search_ancestor(self.tree_name, 'param')
        return params[param_node.position_index]


class ImportName(AbstractNameDefinition):
    start_pos = (1, 0)
    _level = 0

    def __init__(self, parent_context, string_name):
        self._from_module_context = parent_context
        self.string_name = string_name

    def get_qualified_names(self, include_module_names=False):
        if include_module_names:
            if self._level:
                assert self._level == 1, "Everything else is not supported for now"
                module_names = self._from_module_context.string_names
                if module_names is None:
                    return module_names
                return module_names + (self.string_name,)
            return (self.string_name,)
        return ()

    @property
    def parent_context(self):
        m = self._from_module_context
        import_contexts = self.infer()
        if not import_contexts:
            return m
        # It's almost always possible to find the import or to not find it. The
        # importing returns only one context, pretty much always.
        return next(iter(import_contexts))

    @memoize_method
    def infer(self):
        from jedi.evaluate.imports import Importer
        m = self._from_module_context
        return Importer(m.evaluator, [self.string_name], m, level=self._level).follow()

    def goto(self):
        return [m.name for m in self.infer()]

    @property
    def api_type(self):
        return 'module'


class SubModuleName(ImportName):
    _level = 1


class NameWrapper(object):
    def __init__(self, wrapped_name):
        self._wrapped_name = wrapped_name

    @abstractmethod
    def infer(self):
        raise NotImplementedError

    def __getattr__(self, name):
        return getattr(self._wrapped_name, name)

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self._wrapped_name)
