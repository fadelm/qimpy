from __future__ import annotations
from sphinx.application import Sphinx
from docutils.parsers.rst import Directive
from docutils.statemachine import ViewList
from docutils import nodes
from typing import get_type_hints, get_args, get_origin, \
    Dict, List, Tuple, Optional, NamedTuple
from collections.abc import Sequence
from functools import lru_cache
import importlib
import inspect
import os


class YamlDocDirective(Directive):

    has_content = True

    def run(self) -> list:
        rootclass = get_class_from_path(self.content[0])
        constructable = get_class_from_path(self.content[1]
                                            if (len(self.content) > 1)
                                            else 'qimpy.Constructable')

        # Recursively add documentation, one entry per row:
        # --- each row has a command column and a documentation column
        rowlist = []
        self.add_class(rootclass, 0, rowlist, constructable)

        # Collect into a table:
        # --- body
        tbody = nodes.tbody()
        for row in rowlist:
            tbody += row
        # --- group
        tgroup = nodes.tgroup(cols=2)
        for colwidth in (30, 70):
            tgroup += nodes.colspec(colwidth=colwidth)
        tgroup += tbody
        # --- overall table
        table = nodes.table()
        table += tgroup
        return [table]

    def add_class(self, cls: type, level: int, rowlist: List[nodes.row],
                  constructable: type) -> None:
        """Recursively add documentation for class `cls` at depth `level`
        to `rowlist`. Here, `constructable` specifies the base class of all
        objects that are dict/yaml initializable (eg. qimpy.Constructable)."""
        # Get parameter types, documentation and default values:
        assert issubclass(cls, constructable)
        param_types = get_type_hints(cls.__init__)
        header, param_docs = get_parameters(cls.__init__.__doc__)
        signature = inspect.signature(cls.__init__)

        # Report only parameters which have a [YAML] in their docstring:
        for param_name, param_type in param_types.items():
            if param_name in param_docs:
                param_doc = param_docs[param_name]
                if ':yaml:' in param_doc:
                    default_value = signature.parameters[param_name].default
                    default_str = ('' if (default_value is inspect._empty)
                                   else f' {default_value}')
                    pad = '\u00A0' * (2*level)  # using non-breaking spaces
                    # Command cell:
                    cell_cmd = nodes.entry()
                    cell_cmd += nodes.paragraph(text=f'{pad}{param_name}:'
                                                     f'{default_str}')
                    # Replace :yaml: with a link to source code:
                    param_doc = yaml_remove(param_doc)
                    # Documentation cell:
                    cell_doc = nodes.entry()
                    viewlist = ViewList()
                    for i_line, line in enumerate(param_doc.split('\n')):
                        print(i_line, line)
                        viewlist.append(line, 'memory.rst', i_line)
                    self.state.nested_parse(viewlist, 0, cell_doc)
                    # Collect row:
                    row = nodes.row()
                    row += cell_cmd
                    row += cell_doc
                    rowlist.append(row)
                    # Recur down on compound objects:
                    for cls_option in get_args(param_type):
                        if (inspect.isclass(cls_option)
                                and issubclass(cls_option, constructable)):
                            self.add_class(cls_option, level+1, rowlist,
                                           constructable)


def get_class_from_path(full_class_name: str) -> type:
    """Get class from fully-qualified name."""
    # Split class name into module and class:
    full_class_path = full_class_name.split('.')
    module_name = '.'.join(full_class_path[:-1])
    class_name = full_class_path[-1]
    # Import module and get class:
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def get_path_from_class(cls: type) -> str:
    """Get full path of class, getting rid of internal module names."""
    module = cls.__module__
    module_elems = ([] if module is None else (
        [elem for elem in module.split('.')
         if not elem.startswith('_')]))  # drop internal module names
    module_elems.append(cls.__qualname__)
    return '.'.join(module_elems)


def get_parameters(docstr: str) -> Tuple[str, Dict[str, str]]:
    """Parse constructor docstring `docstr` into parameter descriptions.
    Returns header of constructor documentation, and for each parameter."""
    result: Dict[str, str] = {}
    lines = docstr.split('\n')
    parameter_start = len(lines)
    parameter_indent = -1  # indent amount of parameter
    desc_indent = -1  # indent amount of parameter description
    param_name = None
    header = []
    param_desc = []
    for i_line, line_raw in enumerate(lines):
        line = line_raw.lstrip()
        n_indent = len(line_raw) - len(line)
        line = line.rstrip()
        # Detect start of parameters:
        if line == 'Parameters':
            parameter_start = i_line + 2
            parameter_indent = n_indent
        if i_line < parameter_start:
            header.append(line)
            continue
        # Parse parameters:
        if n_indent == parameter_indent:
            # Flush previous parameter, if any:
            if param_name and param_desc:
                result[param_name] = '\n'.join(param_desc).strip('\n')
            # Start new parameter
            param_name = line.split(':')[0].strip()
            param_desc = []
            desc_indent = -1
        elif line:
            if desc_indent == -1:
                desc_indent = n_indent  # based on first line in this desc
            rel_indent = n_indent - desc_indent
            param_desc.append(' '*rel_indent + line)
        else:
            param_desc.append('')  # Blank lines important in ReST formatting

    # Flush last parameter:
    if param_name and param_desc:
        result[param_name] = '\n'.join(param_desc).strip('\n')
    return '\n'.join(header[:-2]), result


PY_TO_YAML = {
    'None': ':yamlkey:`null`',
    'True': ':yamlkey:`yes`',
    'False': ':yamlkey:`no`'
}


YAML_TYPE = {
    str: ':yamltype:`string`',
    float: ':yamltype:`float`',
    int: ':yamltype:`int`',
    bool: ':yamltype:`bool`',
    list: ':yamltype:`list`',
    tuple: ':yamltype:`tuple`',
    type(None): ':yamltype:`null`'
}


SEQUENCE_TYPES = {list, tuple, Sequence}


def yamlify(doc: str) -> str:
    """Replace python keywords with yaml versions in `doc`"""
    for py_word, yaml_word in PY_TO_YAML.items():
        doc = doc.replace(py_word, yaml_word)
    return doc


def yamltype(cls: type) -> str:
    """Return YAML name for type `cls`."""
    result = YAML_TYPE.get(cls, None)
    if result is not None:
        return result
    # Check for sequence types:
    origin = get_origin(cls)
    if origin in SEQUENCE_TYPES:
        result = ':yamltype:`list`'
        args = get_args(cls)
        delim = ' and ' if (origin is tuple) else ' or '
        if len(args):
            result += ' of ' + delim.join(yamltype(arg) for arg in args) + ','
        return result
    # Fallback to python name:
    return str(cls)


def yaml_remove(docstr: str) -> str:
    """Remove :yaml: roles in the input doc version of docstring."""
    key = ':yaml:'
    i_start = docstr.find(key)
    while i_start >= 0:
        i_stop = docstr.find('`', (i_start + len(key) + 1)) + 1
        fullkey = docstr[i_start:i_stop]  # includes `content` after key
        docstr = docstr.replace(fullkey, '')
        # Search for any other keys:
        i_start = docstr.find(key)
    return docstr


def yaml_role(name, rawtext, text, lineno, inliner, options={}, content=[]):
    """Link :yaml: in the python API docs to the input file docs."""
    env = inliner.document.settings.env
    app = env.app
    dest_doc = text
    uri = app.builder.get_relative_uri(env.docname, dest_doc)
    return [nodes.reference(rawtext, '[Input file]',
                            refuri=uri, **options)], []


def yaml_highlight(rolename: str):
    def role(name, rawtext, text, lineno, inliner, options={}, content=[]):
        return [nodes.inline(text=text, classes=['yaml'+rolename])], []
    return role


class Parameter(NamedTuple):
    """Parameter within `ClassInputDoc`."""
    name: str  #: Parameter name
    default: str  #: String representing default value, if any
    summary: str  #: One-line summary
    doc: str  #: Full doc-string.
    classdoc: Optional[ClassInputDoc] = None  #: Documentation of this class
    typename: str = ''  # Name of type (used only if no `classdoc`)


class ClassInputDoc:
    """Input documentation extracted from a `constructable` subclass."""

    def __init__(self, cls: type, constructable: type, app: Sphinx,
                 classdocs: Dict[str, ClassInputDoc], outdir: str) -> None:
        """Write input documentation for `cls` in ReST format within `outdir`.
        Recurs down to parameters marked with :yaml: in `cls.__init__` that
        are also sub-classes of `constructable`. Also adds `self` to classdocs
        to avoid writing `cls`'s documentation multiple times for overlapping
        input documentation trees."""
        self.cls = cls
        self.params: List[Parameter] = []
        self.path = get_path_from_class(cls)
        classdocs[self.path] = self

        assert issubclass(cls, constructable)
        param_types = get_type_hints(cls.__init__)
        header, param_docs = get_parameters(cls.__init__.__doc__)
        signature = inspect.signature(cls.__init__)

        # Process only parameters which have a [YAML] in their docstring:
        for param_name, param_type in param_types.items():
            if param_name in param_docs:
                param_doc = param_docs[param_name]
                if ':yaml:' in param_doc:
                    default_value = signature.parameters[param_name].default
                    default_str = ('' if (default_value is inspect._empty)
                                   else f' {default_value}')
                    param_summary = f'{param_name}:{default_str}'
                    param_doc = yaml_remove(param_doc)
                    typenames = []
                    # Recur down on compound objects:
                    param_class: Optional[ClassInputDoc] = None
                    for cls_option in get_args(param_type):
                        if (inspect.isclass(cls_option)
                                and issubclass(cls_option, constructable)):
                            # Check if already documented cls_option:
                            cls_option_path = get_path_from_class(cls_option)
                            param_class = classdocs.get(cls_option_path, None)
                            # If not, document it now:
                            if param_class is None:
                                param_class = ClassInputDoc(cls_option,
                                                            constructable, app,
                                                            classdocs, outdir)
                        else:
                            typenames.append(yamltype(cls_option))
                    if not typenames:
                        typenames = [yamltype(param_type)]
                    typename = ' or '.join(typenames).rstrip(',')
                    param = Parameter(name=param_name.replace('_', '-'),
                                      default=yamlify(default_str),
                                      summary=yamlify(param_summary),
                                      doc=yamlify(param_doc),
                                      classdoc=param_class,
                                      typename=typename)
                    self.params.append(param)

        # Helper to write a title:
        def write_title(file, title: str, underline: str) -> None:
            file.write(f'{title}\n{underline * len(title)}\n\n')

        # Write ReST file:
        fname = os.path.join(outdir, self.path + '.rst')
        with open(fname, 'w') as fp:
            # Title:
            write_title(fp, f'{cls.__qualname__} input documentation', '=')
            # Constructor header:
            fp.write(header)
            fp.write(f'\n\nUsed to initialize class :class:`{self.path}`.\n\n')
            # Template:
            write_title(fp, 'YAML template:', '-')
            fp.write('.. parsed-literal::\n\n')
            for line in self.get_yaml_template():
                fp.write(f'   {line}\n')
            fp.write('\n')
            # Parameter detailed docs:
            write_title(fp, 'Parameters:', '-')
            for param in self.params:
                write_title(fp, param.name, '+')
                if param.default:
                    fp.write(f'*Default:* {param.default}\n\n')
                if param.classdoc is None:
                    fp.write(f'*Type:* {param.typename}\n\n')
                else:
                    fp.write(f'*Type:* :doc:`{param.classdoc.cls.__qualname__}'
                             f' <{param.classdoc.path}>`\n\n')
                fp.write(param.doc)
                fp.write('\n\n')

    @lru_cache
    def get_yaml_template(self) -> List[str]:
        """Return lines of a yaml template based on parameters.
        Recursively includes templates of component classes within."""
        result = []
        for param in self.params:
            value = ((param.default if param.default
                      else f'[{param.typename}]')
                     if (param.classdoc is None)
                     else '')  # don't put value if class doc follows
            comment = ''  # TODO
            result.append(f':yamlparam:`{param.name}`:{value}{comment}')
            # Recur down to components:
            if param.classdoc is not None:
                pad = '  '
                for line in param.classdoc.get_yaml_template():
                    result.append(pad + line)  # indent component template
        return result


def create_yamldoc_rst_files(app: Sphinx) -> None:
    """Create rst files for each class connected to any yamldoc root class."""
    # Find the doc source files (excluding autogenerated ones):
    env = app.builder.env
    docfiles = [env.doc2path(docname)
                for docname in env.found_docs
                if ((not docname.startswith(('api/', 'yamldoc/')))
                    and os.path.isfile(env.doc2path(docname)))]

    # Find all instances of the yamldoc directive:
    yamldoc_classnames = set()
    directive_key = '.. yamldoc::'
    for docfile in docfiles:
        for line in open(docfile):
            i_start = line.find(directive_key)
            if i_start >= 0:
                tokens = line[(i_start + len(directive_key)):].split()
                yamldoc_classnames.add(tokens[0])
    print('[yamldoc] generating input documentation for root class(es):',
          ', '.join(yamldoc_classnames))

    # Prepare directory for yamldoc class documentations:
    yamldoc_dir = os.path.join(env.srcdir, 'yamldoc')
    if not os.path.isdir(yamldoc_dir):
        os.mkdir(yamldoc_dir)

    # Document all constructable classes recursively:
    constructable = get_class_from_path('qimpy.Constructable')
    classdocs: Dict[str, ClassInputDoc] = {}
    for classname in yamldoc_classnames:
        cls = get_class_from_path(classname)
        ClassInputDoc(cls, constructable, app, classdocs, yamldoc_dir)


def setup(app):
    app.add_directive('yamldoc', YamlDocDirective)
    app.add_role_to_domain('py', 'yaml', yaml_role)
    app.add_role('yamlkey', yaml_highlight('key'))
    app.add_role('yamltype', yaml_highlight('type'))
    app.add_role('yamlparam', yaml_highlight('param'))
    app.add_role('yamlcomment', yaml_highlight('comment'))
    app.connect('builder-inited', create_yamldoc_rst_files)
