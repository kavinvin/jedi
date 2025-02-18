"""
Jedi is a static analysis tool for Python that can be used in IDEs/editors. Its
historic focus is autocompletion, but does static analysis for now as well.
Jedi is fast and is very well tested. It understands Python on a deeper level
than all other static analysis frameworks for Python.

Jedi has support for two different goto functions. It's possible to search for
related names and to list all names in a Python file and infer them. Jedi
understands docstrings and you can use Jedi autocompletion in your REPL as
well.

Jedi uses a very simple API to connect with IDE's. There's a reference
implementation as a `VIM-Plugin <https://github.com/davidhalter/jedi-vim>`_,
which uses Jedi's autocompletion.  We encourage you to use Jedi in your IDEs.
It's really easy.

To give you a simple example how you can use the Jedi library, here is an
example for the autocompletion feature:

>>> import jedi
>>> source = '''
... import json
... json.lo'''
>>> script = jedi.Script(source, 3, len('json.lo'), 'example.py')
>>> script
<Script: 'example.py' ...>
>>> completions = script.completions()
>>> completions
[<Completion: load>, <Completion: loads>]
>>> print(completions[0].complete)
ad
>>> print(completions[0].name)
load

As you see Jedi is pretty simple and allows you to concentrate on writing a
good text editor, while still having very good IDE features for Python.
"""

__version__ = '0.14.0'

def init_speed_hacks(on):
    global speed_hacks
    speed_hacks = on

def speed_hacks():
    global speed_hacks
    return speed_hacks

from jedi.api import Script, Interpreter, set_debug_function, \
    preload_module, names
from jedi import settings
from jedi.api.environment import find_virtualenvs, find_system_environments, \
    get_default_environment, InvalidPythonEnvironment, create_environment, \
    get_system_environment
from jedi.api.exceptions import InternalError
