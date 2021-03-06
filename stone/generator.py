from __future__ import absolute_import, division, print_function, unicode_literals

from abc import ABCMeta, abstractmethod
import argparse
from contextlib import contextmanager
import logging
import os
import six
import textwrap
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    TypeVar,
    Tuple,
)

from stone.api import Api

from stone.lang.tower import doc_ref_re
from stone.data_type import (
    is_alias,
)

# Generic Dict key-val types
K = TypeVar('K')
V = TypeVar('V')

DelimTuple = Tuple[str, str]

def remove_aliases_from_api(api):
    for namespace in api.namespaces.values():
        # Important: Even if this namespace has no aliases, it may reference
        # an alias in an imported namespace.

        # Remove nested aliases first. This way, when we replace an alias with
        # its source later on, it too is alias free.
        for alias in namespace.aliases:
            data_type = alias
            while True:
                # For better or for worse, all non-user-defined types that
                # reference other types do so with a 'data_type' attribute.
                if hasattr(data_type, 'data_type'):
                    if is_alias(data_type.data_type):
                        # Skip the alias (this looks so terrible...)
                        data_type.data_type = data_type.data_type.data_type
                    data_type = data_type.data_type
                else:
                    break

        for data_type in namespace.data_types:
            for field in data_type.fields:
                data_type = field
                while True:
                    if hasattr(data_type, 'data_type'):
                        if is_alias(data_type.data_type):
                            data_type.data_type = data_type.data_type.data_type
                        data_type = data_type.data_type
                    else:
                        break

        for route in namespace.routes:
            if is_alias(route.arg_data_type):
                route.arg_data_type = route.arg_data_type.data_type
            if is_alias(route.result_data_type):
                route.result_data_type = route.result_data_type.data_type
            if is_alias(route.error_data_type):
                route.error_data_type = route.error_data_type.data_type

        # Clear aliases
        namespace.aliases = []
        namespace.alias_by_name = {}

    return api


@six.add_metaclass(ABCMeta)
class Generator(object):
    """
    The parent class for all generators. All generators should extend this
    class to be recognized as such.

    You will want to implement the generate() function to do the generation
    that you need.

    Here's roughly what you need to do in generate().
    1. Use the context manager output_to_relative_path() to specify an output file.

        with output_to_relative_path('generated_code.py'):
            ...

    2. Use the family of emit*() functions to write to the output file.

    The target_folder_path attribute is the path to the folder where all
    generated files should be created.
    """

    # Can be overridden by a subclass
    tabs_for_indents = False

    # Can be overridden with an argparse.ArgumentParser object.
    cmdline_parser = None

    # Can be overridden by a subclass. If true, stone.data_type.Alias
    # objects will be present in the API object. If false, aliases are masked
    # by replacing them with duplicate type definitions as the source type.
    # For backwards compatibility with existing generators defaults to false.
    preserve_aliases = False

    def __init__(self, target_folder_path, args):
        # type: (str, Optional[Sequence[str]]) -> None
        """
        Args:
            target_folder_path (str): Path to the folder where all generated
                files should be created.
        """
        self.logger = logging.getLogger('Generator<%s>' %
                                        self.__class__.__name__)
        self.target_folder_path = target_folder_path
        # Output is a list of strings that should be concatenated together for
        # the final output.
        self.output = []  # type: List[str]
        self.lineno = 1
        self.cur_indent = 0

        self.args = None  # type: Optional[argparse.Namespace]

        if self.cmdline_parser:
            assert isinstance(self.cmdline_parser, argparse.ArgumentParser), (
                'expected cmdline_parser to be ArgumentParser, got %r' %
                self.cmdline_parser)
            try:
                self.args = self.cmdline_parser.parse_args(args)
            except SystemExit:
                print('Note: This is for generator-specific arguments which '
                      'follow arguments to Stone after a "--" delimiter.')
                raise

    @abstractmethod
    def generate(self, api):
        # type: (Api) -> None
        """
        Subclasses should override this method. It's the entry point that is
        invoked by the rest of the toolchain.

        Args:
            api (stone.api.Api): The API specification.
        """
        raise NotImplementedError

    @contextmanager
    def output_to_relative_path(self, relative_path):
        # type: (str) -> Iterator[None]
        """
        Sets up generator so that all emits are directed towards the new file
        created at :param:`relative_path`.

        Clears the output buffer on enter and exit.
        """
        full_path = os.path.join(self.target_folder_path, relative_path)
        directory = os.path.dirname(full_path)
        if not os.path.exists(directory):
            self.logger.info('Creating %s', directory)
            os.makedirs(directory)

        self.logger.info('Generating %s', full_path)
        self.output = []
        yield
        with open(full_path, 'wb') as f:
            f.write(''.join(self.output).encode('utf-8'))
        self.output = []

    def output_buffer_to_string(self):
        # type: () -> str
        """Returns the contents of the output buffer as a string."""
        return ''.join(self.output)

    def clear_output_buffer(self):
        self.output = []

    @contextmanager
    def indent(self, dent=None):
        # type: (Optional[int]) -> Iterator[None]
        """
        For the duration of the context manager, indentation will be increased
        by dent. Dent is in units of spaces or tabs depending on the value of
        the class variable tabs_for_indents. If dent is None, indentation will
        increase by either four spaces or one tab.
        """
        assert dent is None or dent > 0, 'dent must be a whole number.'
        if dent is None:
            if self.tabs_for_indents:
                dent = 1
            else:
                dent = 4
        self.cur_indent += dent
        yield
        self.cur_indent -= dent

    def make_indent(self):
        # type: () -> str
        """
        Returns a string representing the current indentation. Indents can be
        either spaces or tabs, depending on the value of the class variable
        tabs_for_indents.
        """
        if self.tabs_for_indents:
            return '\t' * self.cur_indent
        else:
            return ' ' * self.cur_indent

    def emit_raw(self, s):
        # type: (str) -> None
        """
        Adds the input string to the output buffer. The string must end in a
        newline. It may contain any number of newline characters. No
        indentation is generated.
        """
        self.lineno += s.count('\n')
        self.output.append(s)
        if len(s) > 0 and s[-1] != '\n':
            raise AssertionError(
                'Input string to emit_raw must end with a newline.')

    def emit(self, s=''):
        # type: (str) -> None
        """
        Adds indentation, then the input string, and lastly a newline to the
        output buffer. If s is an empty string (default) then an empty line is
        created with no indentation.
        """
        assert isinstance(s, six.text_type), 's must be a unicode string'
        assert '\n' not in s, \
            'String to emit cannot contain newline strings.'
        if s:
            self.emit_raw('%s%s\n' % (self.make_indent(), s))
        else:
            self.emit_raw('\n')

    def emit_wrapped_text(
            self,
            s,                       # type: str
            prefix='',               # type: str
            initial_prefix='',       # type: str
            subsequent_prefix='',    # type: str
            width=80,                # type: int
            break_long_words=False,  # type: bool
            break_on_hyphens=False   # type: bool
    ):
        # type: (...) -> None
        """
        Adds the input string to the output buffer with indentation and
        wrapping. The wrapping is performed by the :func:`textwrap.fill` Python
        library function.

        Args:
            s (str): The input string to wrap.
            prefix (str): The string to prepend to *every* line.
            initial_prefix (str): The string to prepend to the first line of
                the wrapped string. Note that the current indentation is
                already added to each line.
            subsequent_prefix (str): The string to prepend to every line after
                the first. Note that the current indentation is already added
                to each line.
            width (int): The target width of each line including indentation
                and text.
            break_long_words (bool): Break words longer than width.  If false,
                those words will not be broken, and some lines might be longer
                than width.
            break_on_hyphens (bool): Allow breaking hyphenated words. If true,
                wrapping will occur preferably on whitespaces and right after
                hyphens part of compound words.
        """
        indent = self.make_indent()
        prefix = indent + prefix

        self.emit_raw(textwrap.fill(s,  # type: ignore
                                        # (Can be removed after github.com/python/typeshed/pull/420)
                                    initial_indent=prefix+initial_prefix,
                                    subsequent_indent=prefix+subsequent_prefix,
                                    width=width,
                                    break_long_words=break_long_words,
                                    break_on_hyphens=break_on_hyphens,
                                    ) + '\n')

    @classmethod
    def process_doc(cls, doc, handler):
        # type: (str, Callable[[str, str], str]) -> str
        """
        Helper for parsing documentation references in Stone docstrings and
        replacing them with more suitable annotations for the generated output.

        Args:
            doc (str): A Stone docstring.
            handler: A function with the following signature:
                `(tag: str, value: str) -> str`. It will be called for every
                reference found in the docstring with the tag and value parsed
                for you. The returned string will be substituted in the
                docstring in place of the reference.
        """
        assert isinstance(doc, six.text_type), \
            'Expected string (unicode in PY2), got %r.' % type(doc)
        cur_index = 0
        parts = []
        for match in doc_ref_re.finditer(doc):
            # Append the part of the doc that is not part of any reference.
            start, end = match.span()
            parts.append(doc[cur_index:start])
            cur_index = end

            # Call the handler with the next tag and value.
            tag = match.group('tag')
            val = match.group('val')
            sub = handler(tag, val)
            parts.append(sub)
        parts.append(doc[cur_index:])
        return ''.join(parts)


class CodeGenerator(Generator):
    """
    Extend this instead of :class:`Generator` when generating source code.
    Contains helper functions specific to code generation.
    """

    def filter_out_none_valued_keys(self, d):
        # type: (Dict[K, V]) -> Dict[K, V]
        """Given a dict, returns a new dict with all the same key/values except
        for keys that had values of None."""
        new_d = {}
        for k, v in d.items():
            if v is not None:
                new_d[k] = v
        return new_d

    def generate_multiline_list(
        self,
        items,               # type: List[str]
        before='',           # type: str
        after='',            # type: str
        delim=('(', ')'),    # type: DelimTuple
        compact=True,        # type: bool
        sep=',',             # type: str
        skip_last_sep=False  # type: bool
    ):
        # type: (...) -> None
        """
        Given a list of items, emits one item per line.

        This is convenient for function prototypes and invocations, as well as
        for instantiating arrays, sets, and maps in some languages.

        TODO(kelkabany): A generator that uses tabs cannot be used with this
            if compact is false.

        Args:
            items (list[str]): Should contain the items to generate a list of.
            before (str): The string to come before the list of items.
            after (str): The string to follow the list of items.
            delim (str, str): The first element is added immediately following
                `before`. The second element is added prior to `after`.
            compact (bool): In compact mode, the enclosing parentheses are on
                the same lines as the first and last list item.
            sep (str): The string that follows each list item when compact is
                true. If compact is false, the separator is omitted for the
                last item.
            skip_last_sep (bool): When compact is false, whether the last line
                should have a trailing separator. Ignored when compact is true.
        """
        assert len(delim) == 2 and isinstance(delim[0], six.text_type) and \
            isinstance(delim[1], six.text_type), 'delim must be a tuple of two unicode strings.'

        if len(items) == 0:
            self.emit(before + delim[0] + delim[1] + after)
            return
        if len(items) == 1:
            self.emit(before + delim[0] + items[0] + delim[1] + after)
            return

        if compact:
            self.emit(before + delim[0] + items[0] + sep)
            def emit_list(items):
                items = items[1:]
                for (i, item) in enumerate(items):
                    if i == len(items) - 1:
                        self.emit(item + delim[1] + after)
                    else:
                        self.emit(item + sep)
            if before or delim[0]:
                with self.indent(len(before) + len(delim[0])):
                    emit_list(items)
            else:
                emit_list(items)
        else:
            if before or delim[0]:
                self.emit(before + delim[0])
            with self.indent():
                for (i, item) in enumerate(items):
                    if i == len(items) - 1 and skip_last_sep:
                        self.emit(item)
                    else:
                        self.emit(item + sep)
            if delim[1] or after:
                self.emit(delim[1] + after)
            elif delim[1]:
                self.emit(delim[1])

    @contextmanager
    def block(
        self,
        before='',        # type: str
        after='',         # type: str
        delim=('{','}'),  # type: DelimTuple
        dent=None,        # type: Optional[int]
        allman=False      # type: bool
    ):
        # type: (...) -> Iterator[None]
        """
        A context manager that emits configurable lines before and after an
        indented block of text.

        This is convenient for class and function definitions in some
        languages.

        Args:
            before (str): The string to be output in the first line which is
                not indented..
            after (str): The string to be output in the last line which is
                not indented.
            delim (str, str): The first element is added immediately following
                `before` and a space. The second element is added prior to a
                space and then `after`.
            dent (int): The amount to indent the block. If none, the default
                indentation increment is used (four spaces or one tab).
            allman (bool): Indicates whether to use `Allman` style indentation,
                or the default `K&R` style. If there is no `before` string this
                is ignored. For more details about indent styles see
                http://en.wikipedia.org/wiki/Indent_style
        """
        assert len(delim) == 2, 'delim must be a tuple of length 2'
        assert (isinstance(delim[0], (six.text_type, type(None))) and
                isinstance(delim[1], (six.text_type, type(None)))), (
            'delim must be a tuple of two optional strings.')

        if before and not allman:
            if delim[0] is not None:
                self.emit('{} {}'.format(before, delim[0]))
            else:
                self.emit(before)
        else:
            if before:
                self.emit(before)
            if delim[0] is not None:
                self.emit(delim[0])

        with self.indent(dent):
            yield

        if delim[1] is not None:
            self.emit(delim[1] + after)
        else:
            self.emit(after)
