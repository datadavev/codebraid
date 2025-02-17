# -*- coding: utf-8 -*-
#
# Copyright (c) 2018-2019, Geoffrey M. Poore
# All rights reserved.
#
# Licensed under the BSD 3-Clause License:
# http://opensource.org/licenses/BSD-3-Clause
#


import collections
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import tempfile
import textwrap
from typing import List, Optional, Sequence, Tuple, Union
from .base import CodeChunk, Converter, Include
from .. import err
from .. import util


# Pandoc node types mapped to integers representing the layout of node
# contents (if any).
# https://github.com/jgm/pandocfilters/blob/master/pandocfilters.py
pandoc_block_nodes = {
    'Plain': 1,
    'Para': 1,
    'CodeBlock': 2,
    'RawBlock': 2,
    'BlockQuote': 1,
    'OrderedList': 2,
    'BulletList': 1,
    'DefinitionList': 1,
    'Header': 3,
    'HorizontalRule': 0,
    'Table': 5,
    'Div': 2,
    'Null': 0,
}
pandoc_inline_nodes = {
    'Str': 1,
    'Emph': 1,
    'Strong': 1,
    'Strikeout': 1,
    'Superscript': 1,
    'Subscript': 1,
    'SmallCaps': 1,
    'Quoted': 2,
    'Cite': 2,
    'Code': 2,
    'Space': 0,
    'LineBreak': 0,
    'Math': 2,
    'RawInline': 2,
    'Link': 3,
    'Image': 3,
    'Note': 1,
    'SoftBreak': 0,
    'Span': 2,
}
pandoc_nodes = {**pandoc_block_nodes, **pandoc_inline_nodes}
pandoc_code_nodes = {k: v for k, v in pandoc_nodes.items() if k.startswith('Code')}
pandoc_raw_nodes = {k: v for k, v in pandoc_nodes.items() if k.startswith('Raw')}
pandoc_code_raw_nodes = {**pandoc_code_nodes, **pandoc_raw_nodes}




class PandocError(err.CodebraidError):
    pass




def _get_class_processors():
    '''
    Create dict mapping Pandoc classes to processing functions.

    Duplicate or invalid options related to presentation result in warnings,
    while duplicate or invalid options related to code execution result in
    errors.
    '''
    def class_lang_or_unknown(code_chunk, options, class_index, class_name):
        if 'lang' in options:
            if class_name.startswith('cb.'):
                code_chunk.source_errors.append('Unknown or unsupported Codebraid command "{0}"'.format(class_name))
            else:
                code_chunk.source_errors.append('Unknown non-Codebraid class')
        elif class_index > 0:
            if class_name.startswith('cb.'):
                code_chunk.source_errors.append('Unknown or unsupported Codebraid command "{0}"'.format(class_name))
            else:
                code_chunk.source_errors.append('The language/format "{0}" must be the first class for code chunk'.format(class_name))
        else:
            options['lang'] = class_name

    def class_codebraid_command(code_chunk, options, class_index, class_name):
        if 'codebraid_command' in options:
            code_chunk.source_errors.append('Only one Codebraid command can be applied per code chunk')
        else:
            options['codebraid_command'] = class_name.split('.', 1)[1]

    def class_line_anchors(code_chunk, options, class_index, class_name):
        if 'line_anchors' in options:
            code_chunk.source_warnings.append('Duplicate line anchor class for code chunk')
        else:
            options['line_anchors'] = True

    def class_line_numbers(code_chunk, options, class_index, class_name):
        if 'line_numbers' in options:
            code_chunk.source_warnings.append('Duplicate line numbering class for code chunk')
        else:
            options['line_numbers'] = True

    codebraid_commands = {'cb.{}'.format(k): class_codebraid_command for k in CodeChunk.commands}
    line_anchors = {k: class_line_anchors for k in ('lineAnchors', 'line-anchors', 'line_anchors')}
    line_numbers = {k: class_line_numbers for k in ('line_numbers', 'numberLines', 'number-lines', 'number_lines')}
    return collections.defaultdict(lambda: class_lang_or_unknown,
                                   {**codebraid_commands,
                                    **line_anchors,
                                    **line_numbers})


def _get_keyval_processors():
    '''
    Create dict mapping Pandoc key-value attributes to processing functions.
    '''
    # Options like `include` have sub-options, which need to be translated
    # into a dict.  In a markup language with more expressive option syntax,
    # this would be supported as `include={key=value, ...}`.  For Pandoc
    # Markdown, the alternatives are keys like `include.file` or
    # `include_file`.  In either case, the base option name `include`
    # functions as a namespace and needs to be protected from being
    # overwritten by an invalid option.  Track those namespaces with a set.
    namespace_keywords = set()

    def keyval_generic(code_chunk, options, key, value):
        if key in options:
            code_chunk.source_errors.append('Duplicate "{0}" attribute for code chunk'.format(key))
        elif key in namespace_keywords:
            code_chunk.source_errors.append('Invalid key "{0}" for code chunk'.format(key))
        else:
            options[key] = value

    def keyval_bool(code_chunk, options, key, value):
        if key in options:
            code_chunk.source_errors.append('Duplicate "{0}" attribute for code chunk'.format(key))
        elif value not in ('true', 'false'):
            code_chunk.source_errors.append('Attribute "{0}" must be true or false for code chunk'.format(key))
        else:
            options[key] = value == 'true'

    def keyval_int(code_chunk, options, key, value):
        if key in options:
            code_chunk.source_errors.append('Duplicate "{0}" attribute for code chunk'.format(key))
        else:
            try:
                value = int(value)
            except (ValueError, TypeError):
                code_chunk.source_errors.append('Attribute "{0}" must be integer for code chunk'.format(key))
            else:
                options[key] = value

    def keyval_first_number(code_chunk, options, key, value):
        if 'first_number' in options:
            code_chunk.source_warnings.append('Duplicate first line number attribute for code chunk')
        else:
            try:
                value = int(value)
            except (ValueError, TypeError):
                pass
            options['first_number'] = value

    def keyval_line_anchors(code_chunk, options, key, value):
        if 'line_anchors' in options:
            code_chunk.source_warnings.append('Duplicate line anchor attribute for code chunk')
        elif value not in ('true', 'false'):
            code_chunk.source_warnings.append('Attribute "{0}" must be true or false for code chunk'.format(key))
        else:
            options['line_anchors'] = value == 'true'

    def keyval_line_numbers(code_chunk, options, key, value):
        if 'line_numbers' in options:
            code_chunk.source_warnings.append('Duplicate line numbering attribute for code chunk')
        elif value not in ('true', 'false'):
            code_chunk.source_warnings.append('Attribute "{0}" must be true or false for code chunk'.format(key))
        else:
            options['line_numbers'] = value == 'true'

    def keyval_namespace(code_chunk, options, key, value):
        if '.' in key:
            namespace, sub_key = key.split('.', 1)
        else:
            namespace, sub_key = key.split('_', 1)
        if namespace in options:
            sub_options = options[namespace]
        else:
            sub_options = {}
            options[namespace] = sub_options
        if sub_key in sub_options:
            code_chunk.source_errors.append('Duplicate "{0}" attribute for code chunk'.format(key))
        else:
            sub_options[sub_key] = value

    expand_tabs = {k: keyval_bool for k in ['{0}_expand_tabs'.format(dsp) if dsp else 'expand_tabs'
                                            for dsp in ('', 'markup', 'copied_markup', 'code', 'stdout', 'stderr')]}
    first_number = {k: keyval_first_number for k in ('first_number', 'startFrom', 'start-from', 'start_from')}
    include = {'include_{0}'.format(k): keyval_namespace for k in Include.keywords}
    line_anchors = {k: keyval_line_anchors for k in ('lineAnchors', 'line-anchors', 'line_anchors')}
    line_numbers = {k: keyval_line_numbers for k in ('line_numbers', 'numberLines', 'number-lines', 'number_lines')}
    rewrap_lines = {k: keyval_bool for k in ['{0}_rewrap_lines'.format(dsp) if dsp else 'rewrap_lines'
                                             for dsp in ('', 'markup', 'copied_markup', 'code', 'stdout', 'stderr')]}
    rewrap_width = {k: keyval_int for k in ['{0}_rewrap_width'.format(dsp) if dsp else 'rewrap_width'
                                            for dsp in ('', 'markup', 'copied_markup', 'code', 'stdout', 'stderr')]}
    tab_size = {k: keyval_int for k in ['{0}_tab_size'.format(dsp) if dsp else 'tab_size'
                                         for dsp in ('', 'markup', 'copied_markup', 'code', 'stdout', 'stderr')]}
    namespace_keywords.add('include')
    return collections.defaultdict(lambda: keyval_generic,
                                   {'complete': keyval_bool,
                                    'copy': keyval_generic,
                                    'example': keyval_bool,
                                    **expand_tabs,
                                    **first_number,
                                    **include,
                                    'jupyter_timeout': keyval_int,
                                    **line_anchors,
                                    **line_numbers,
                                    'live_output': keyval_bool,
                                    'name': keyval_generic,
                                    'outside_main': keyval_bool,
                                    **rewrap_lines,
                                    **rewrap_width,
                                    **tab_size})




class PandocCodeChunk(CodeChunk):
    '''
    Code chunk for Pandoc Markdown.
    '''
    def __init__(self,
                 node: dict,
                 parent_node: dict,
                 parent_node_list: list,
                 parent_node_list_index: int,
                 source_name: Optional[str]=None,
                 source_start_line_number: Optional[int]=None):
        super().__pre_init__()

        self.node = node
        self.parent_node = parent_node
        self.parent_node_list = parent_node_list
        self.parent_node_list_index = parent_node_list_index

        node_id, node_classes, node_kvpairs = node['c'][0]
        self.node_id = node_id
        self.node_classes = node_classes
        self.node_kvpairs = node_kvpairs
        code = node['c'][1]
        options = {}

        # Preprocess options
        inline = node['t'] == 'Code'
        for n, c in enumerate(node_classes):
            self._class_processors[c](self, options, n, c)
        for k, v in node_kvpairs:
            self._kv_processors[k](self, options, k, v)
        # All processed data from classes and key-value pairs is stored in
        # `options`, but only some of these are valid Codebraid options.
        # Remove those that are not and store in temp variables.
        codebraid_command = options.pop('codebraid_command', None)
        line_anchors = options.pop('lineAnchors', None)

        # Process options
        super().__init__(codebraid_command, code, options, source_name=source_name,
                         source_start_line_number=source_start_line_number, inline=inline)

        # Work with processed options -- now use `self.options`
        if inline and self.options['example']:
            if parent_node['t'] not in ('Plain', 'Para') or 'codebraid_pseudonode' in parent_node or len(parent_node_list) > 1:
                self.source_errors.insert(0, 'Option "example" is only allowed for inline code that is in a paragraph by itself')
                self.options['example'] = False
        pandoc_id = node_id
        pandoc_classes = []
        pandoc_kvpairs = []
        lang = options.get('lang', None)
        if lang is not None:
            if lang.endswith('_repl'):
                lang = lang.rsplit('_')[0]
            pandoc_classes.append(lang)
            if self.command == 'repl':
                pandoc_classes.append('repl')
        if line_anchors:
            pandoc_classes.append('lineAnchors')
        if self.options.get('code_line_numbers', False):
            pandoc_classes.append('numberLines')
        # Can't handle `startFrom` yet here, because if it is `next`, then
        # the value depends on which other code chunks end up in the session.
        # Starting line number is determined when output is generated.

        self.pandoc_id = pandoc_id
        self.pandoc_classes = pandoc_classes
        self.pandoc_kvpairs = pandoc_kvpairs
        self._output_nodes = None
        self._as_markup_lines = None
        self._as_example_markup_lines = None


    _class_processors = _get_class_processors()
    _kv_processors = _get_keyval_processors()

    # This may need additional refinement in future depending on allowed values
    _unquoted_kv_value_re = re.compile(r'[A-Za-z$_+\-][A-Za-z0-9$_+\-:]*')


    def finalize_after_copy(self):
        if self.options['lang'] is None:
            self.pandoc_classes.insert(0, self.copy_chunks[0].options['lang'])
        self.options.finalize_after_copy()


    @property
    def output_nodes(self):
        '''
        A list of nodes representing output, or representing source errors if
        those prevented execution.
        '''
        if self._output_nodes is not None:
            return self._output_nodes
        nodes = []
        if self.source_errors:
            if self.runtime_source_error:
                message = 'RUNTIME SOURCE ERROR in "{0}" near line {1}:'.format(self.source_name, self.source_start_line_number)
            else:
                message = 'SOURCE ERROR in "{0}" near line {1}:'.format(self.source_name, self.source_start_line_number)
            if self.inline:
                nodes.append({'t': 'Code', 'c': [['', ['sourceError'], []], '{0} {1}'.format(message, ' '.join(self.source_errors))]})
            else:
                nodes.append({'t': 'CodeBlock', 'c': [['', ['sourceError'], []], '{0}\n{1}'.format(message, '\n'.join(self.source_errors))]})
            self._output_nodes = nodes
            return nodes
        if not self.inline and self.options['code_line_numbers']:
            # Line numbers can't be precalculated since they are determined by
            # how a session is assembled across potentially multiple sources
            first_number = self.options['code_first_number']
            if first_number == 'next':
                first_number = str(self.code_start_line_number)
            else:
                first_number = str(first_number)
            self.pandoc_kvpairs.append(['startFrom', first_number])
        t_code = 'Code' if self.inline else 'CodeBlock'
        t_raw = 'RawInline' if self.inline else 'RawBlock'
        unformatted_nodes = []
        for output, format in self.options['show'].items():
            if output in ('markup', 'copied_markup'):
                unformatted_nodes.append({'t': t_code, 'c': [['', ['markdown'], []], self.layout_output(output, format)]})
            elif output == 'code':
                unformatted_nodes.append({'t': t_code, 'c': [[self.pandoc_id, self.pandoc_classes, self.pandoc_kvpairs], self.layout_output(output, format)]})
            elif output == 'repl':
                if self.repl_lines is None:
                    continue
                unformatted_nodes.append({'t': t_code, 'c': [[self.pandoc_id, self.pandoc_classes, self.pandoc_kvpairs], self.layout_output(output, format)]})
            elif output in ('expr', 'stdout', 'stderr'):
                if format == 'verbatim':
                    if getattr(self, output+'_lines') is not None:
                        unformatted_nodes.append({'t': t_code, 'c': [['', [output], []], self.layout_output(output, format)]})
                elif format == 'verbatim_or_empty':
                    unformatted_nodes.append({'t': t_code, 'c': [['', [output], []], self.layout_output(output, format)]})
                elif format == 'raw':
                    if getattr(self, output+'_lines') is not None:
                        unformatted_nodes.append({'t': t_raw, 'c': ['markdown', self.layout_output(output, format)]})
                else:
                    raise ValueError
            elif output == 'rich_output':
                if self.rich_output is None:
                    continue
                for ro in self.rich_output:
                    ro_data = ro['data']
                    ro_files = ro['files']
                    for fmt_and_text_display in format:
                        if ':' in fmt_and_text_display:
                            fmt, fmt_text_display = fmt_and_text_display.split(':')
                        else:
                            fmt = fmt_and_text_display
                            fmt_text_display = self.options.rich_text_default_display.get(fmt)
                        fmt_mime_type = self.options.mime_map[fmt]
                        if fmt_mime_type not in ro_data:
                            continue
                        if fmt_mime_type in ro_files:
                            image_node = {'t': 'Image', 'c': [['', [], []], [], [ro_files[fmt_mime_type], '']]}
                            if self.inline:
                                unformatted_nodes.append(image_node)
                            else:
                                para_node = {'t': 'Para', 'c': [image_node]}
                                unformatted_nodes.append(para_node)
                            break
                        data = ro_data[fmt_mime_type]
                        lines = util.splitlines_lf(data)
                        if fmt in ('latex', 'html', 'markdown'):
                            if fmt == 'latex':
                                raw_fmt = 'tex'
                            else:
                                raw_fmt = fmt
                            if fmt_text_display == 'raw':
                                unformatted_nodes.append({'t': t_raw, 'c': [raw_fmt, self.layout_output(output, 'raw', lines)]})
                            elif fmt_text_display == 'verbatim':
                                if lines:
                                    unformatted_nodes.append({'t': t_code, 'c': [['', [fmt], []], self.layout_output(output, 'verbatim', lines)]})
                            elif fmt_text_display == 'verbatim_or_empty':
                                unformatted_nodes.append({'t': t_code, 'c': [['', [fmt], []], self.layout_output(output, 'verbatim', lines)]})
                            else:
                                raise ValueError
                            break
                        if fmt == 'plain':
                            if fmt_text_display == 'raw':
                                unformatted_nodes.append({'t': t_raw, 'c': ['markdown', self.layout_output(output, fmt_text_display, lines)]})
                            elif fmt_text_display == 'verbatim':
                                if lines:
                                    unformatted_nodes.append({'t': t_code, 'c': [['', [], []], self.layout_output(output, 'verbatim', lines)]})
                            elif fmt_text_display == 'verbatim_or_empty':
                                unformatted_nodes.append({'t': t_code, 'c': [['', [], []], self.layout_output(output, 'verbatim', lines)]})
                            else:
                                raise ValueError
                            break
                        raise ValueError
            else:
                raise ValueError
        if unformatted_nodes:
            # Prevent adjacent nodes from merging unintentionally when
            # converted through intermediate Markdown
            for node, next_node in zip(unformatted_nodes[:-1], unformatted_nodes[1:]):
                nodes.append(node)
                if node['t'] == t_raw or next_node['t'] == t_raw:
                    if self.inline:
                        nodes.append({'t': 'Str', 'c': ' '})
                    else:
                        nodes.append({'t': 'Para', 'c': []})
                elif self.inline and node['t'] == next_node['t'] == t_code:
                    nodes.append({'t': 'Str', 'c': ' '})
            nodes.append(unformatted_nodes[-1])
        self._output_nodes = nodes
        return nodes


    @property
    def as_markup_lines(self):
        if self._as_markup_lines is not None:
            return self._as_markup_lines
        self._as_markup_lines = self._generate_markdown_lines()
        return self._as_markup_lines

    @property
    def as_example_markup_lines(self):
        if self._as_example_markup_lines is not None:
            return self._as_example_markup_lines
        self._as_example_markup_lines = self._generate_markdown_lines(example=True)
        return self._as_example_markup_lines

    def _generate_markdown_lines(self, example=False):
        '''
        Generate an approximation of the Markdown source that created the
        node.  This is used with `show=markup` and in creating examples that
        show Markdown source plus output.
        '''
        attr_list = []
        if self.node_id:
            attr_list.append('#{0}'.format(self.node_id))
        for c in self.node_classes:
            attr_list.append('.{0}'.format(c))
        hide_keys = set(self.options.get('hide_markup_keys', []))
        if example:
            hide_keys.add('example')
        for k, v in self.node_kvpairs:
            if k not in hide_keys:
                # Valid keys don't need quoting, some values may
                if not self._unquoted_kv_value_re.match(v):
                    v = '"{0}"'.format(v.replace('\\', '\\\\').replace('"', '\\"'))
                attr_list.append('{0}={1}'.format(k, v))
        if self.placeholder_code_lines is not None:
            code_lines = self.placeholder_code_lines
            code = code_lines[0]
        else:
            code_lines = self.code_lines
            code = self.code
        if self.inline:
            code_strip = code.strip(' ')
            if code_strip.startswith('`'):
                code = ' ' + code
            if code_strip.endswith('`'):
                code = code + ' '
            delim = '`'
            while delim in code:
                delim += '`'
            md_lines = ['{delim}{code}{delim}{{{attr}}}'.format(delim=delim, code=code, attr=' '.join(attr_list))]
        elif self.placeholder_code_lines is None or code:
            delim = '```'
            while delim in code:
                delim += '```'
            md_lines = ['{delim}{{{attr}}}'.format(delim=delim, attr=' '.join(attr_list)), *code_lines, delim]
        else:
            md_lines = ['```{{{attr}}}'.format(attr=' '.join(attr_list)), '```']
        return md_lines


    def update_parent_node(self):
        '''
        Update parent node with output.
        '''
        if not self.options['example']:
            index = self.parent_node_list_index
            self.parent_node_list[index:index+1] = self.output_nodes
            return
        markup_node = {'t': 'CodeBlock', 'c': [['', [], []], self.layout_output('example_markup', 'verbatim')]}
        example_div_contents = [{'t': 'Div', 'c': [['', ['exampleMarkup'], []], [markup_node]]}]
        output_nodes = self.output_nodes
        if output_nodes:
            if self.inline:
                # `output_nodes` are all inline, but will be inserted into a
                # div, so need a block-level wrapper
                output_nodes = [{'t': 'Para', 'c': output_nodes}]
            example_div_contents.append({'t': 'Div', 'c': [['', ['exampleOutput'], []], output_nodes]})
        example_div_node = {'t': 'Div', 'c': [['', ['example'], []], example_div_contents]}
        if self.inline:
            # An inline node can't be replaced with a div directly.  Instead,
            # its block-level parent node is redefined.  The validity of this
            # operation is checked during initialization.
            parent_para_plain_node = self.parent_node
            parent_para_plain_node['t'] = 'Div'
            parent_para_plain_node['c'] = example_div_node['c']
        else:
            index = self.parent_node_list_index
            self.parent_node_list[index] = example_div_node




def _get_walk_closure(enumerate=enumerate, isinstance=isinstance, list=list, dict=dict):
    def walk_node_list(node_list, parent_node, type_filter=None, skip_note_contents=False, in_note=False):
        '''
        Walk all AST nodes in a list, recursively descending to walk all child
        nodes as well.  The walk function is written so that it is only ever
        called on lists, reducing recursion depth and reducing the number of
        times the walk function is called.  Thus, it is never called on `Str`
        nodes and other leaf nodes, which will typically make up the vast
        majority of nodes.

        DefinitionLists are handled specially to wrap terms in fake Plain
        nodes, which are marked so that they can be identified later if
        necessary.  This simplifies processing.

        Yields a tuple containing a node, its parent node, parent list,
        parent list index, and a boolean indicating whether the node is inside
        a note.

        If `type_filter` is provided, it must be an object like a `set()` that
        supports membership checks via `in`.  Only nodes with types in
        `type_filter` will be yielded.

        If `skip_notes` is true, recursion skips nodes inside notes.
        '''
        for index, obj in enumerate(node_list):
            if isinstance(obj, dict):
                try:
                    node_type = obj['t']
                except KeyError:
                    continue
                if type_filter is None or node_type in type_filter:
                    yield (obj, parent_node, node_list, index, in_note)
                if 'c' in obj:
                    obj_contents = obj['c']
                    if isinstance(obj_contents, list):
                        if node_type == 'Note':
                            if skip_note_contents:
                                continue
                            in_note = True
                            yield from walk_node_list(obj_contents, obj, type_filter, skip_note_contents, in_note)
                        elif node_type != 'DefinitionList':
                            yield from walk_node_list(obj_contents, obj, type_filter, skip_note_contents, in_note)
                        else:
                            for elem in obj_contents:
                                term, definition = elem
                                pseudonode = {'t': 'Plain', 'c': term, 'codebraid_pseudonode': True}
                                if type_filter is None or 'Plain' in type_filter:
                                    yield (pseudonode, obj, elem, 0, in_note)
                                yield from walk_node_list(term, pseudonode, type_filter, skip_note_contents, in_note)
                                yield from walk_node_list(definition, obj, type_filter, skip_note_contents, in_note)
            elif isinstance(obj, list):
                yield from walk_node_list(obj, parent_node, type_filter, skip_note_contents, in_note)
    return walk_node_list
walk_node_list = _get_walk_closure()




class PandocConverter(Converter):
    '''
    Converter based on Pandoc (https://pandoc.org/).

    Pandoc is used to parse input into a JSON-based AST.  Code nodes in the
    AST are located, processed, and then replaced.  Next, the AST is converted
    back into the original input format (or possibly another format), and
    reparsed into a new AST.  This allows raw code output to be interpreted as
    markup.  Finally, the new AST can be converted into the output format.
    '''
    def __init__(self, *,
                 pandoc_path: Optional[Union[str, pathlib.Path]]=None,
                 pandoc_file_scope: Optional[bool]=False,
                 from_format: Optional[str]=None,
                 scroll_sync: bool=False,
                 **kwargs):
        if from_format is not None and not isinstance(from_format, str):
            raise TypeError
        from_format, from_format_pandoc_extensions = self._split_format_extensions(from_format)

        super().__init__(from_format=from_format, **kwargs)

        if pandoc_path is None:
            pandoc_path = pathlib.Path('pandoc')
        else:
            pandoc_path = pathlib.Path(pandoc_path)
            if self.expandvars:
                pandoc_path = pathlib.Path(os.path.expandvars(pandoc_path))
            if self.expanduser:
                pandoc_path = pandoc_path.expanduser()
        try:
            proc = subprocess.run([str(pandoc_path), '--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except FileNotFoundError:
            raise RuntimeError('Pandoc path "{0}" does not exist'.format(pandoc_path))
        pandoc_version_match = re.search(rb'\d+\.\d+', proc.stdout)
        if not pandoc_version_match:
            raise RuntimeError('Could not determine Pandoc version from "{0} --version"; faulty Pandoc installation?'.format(pandoc_path))
        pandoc_version_major, pandoc_version_minor = (float(x) for x in pandoc_version_match.group().split(b'.', 1))
        if pandoc_version_major < 2 or pandoc_version_minor < 4:
            raise RuntimeError('Pandoc at "{0}" is version {1}.{2}, but >= 2.4 is required'.format(pandoc_path, pandoc_version_major, pandoc_version_minor))
        self.pandoc_path = pandoc_path
        if platform.system() == 'Windows':
            pandoc_template_path = pathlib.Path('~/AppData/Roaming/pandoc/templates').expanduser()
        else:
            pandoc_data_path = pathlib.Path(os.environ.get('XDG_DATA_HOME', '~/.local/share')).expanduser() / 'pandoc'
            if not pandoc_data_path.is_dir():
                pandoc_data_path = pathlib.Path('~/.pandoc').expanduser()
            pandoc_template_path = pandoc_data_path / 'templates'
        if not pandoc_template_path.is_dir():
            pandoc_template_path.mkdir(parents=True)
        codebraid_markdown_roundtrip_template_path = pandoc_template_path / 'codebraid_pandoc_markdown_roundtrip.md'
        if not codebraid_markdown_roundtrip_template_path.is_file():
            codebraid_markdown_roundtrip_template_path.write_text(textwrap.dedent('''\
                $if(titleblock)$
                $titleblock$

                $endif$
                $body$
                '''), encoding='utf8')
        self.codebraid_markdown_roundtrip_template_path = codebraid_markdown_roundtrip_template_path

        if not isinstance(pandoc_file_scope, bool):
            raise TypeError
        if pandoc_file_scope and kwargs.get('cross_source_sessions') is None:
            # `pandoc_file_scope` automatically disables `cross_source_sessions`
            # unless `cross_source_sessions` is explicitly specified.
            self.cross_source_sessions = False
        self.pandoc_file_scope = pandoc_file_scope

        if self.from_format == 'markdown' and not pandoc_file_scope and len(self.sources) > 1:
            # If multiple files are being passed to Pandoc for concatenated
            # processing, ensure sufficient whitespace to prevent elements in
            # different files from merging, and insert a comment to prevent
            # indented elements from merging.  This means that the original
            # sources cannot be passed to Pandoc directly.
            for source_name, source_string in self.sources.items():
                if source_string[-1:] == '\n':
                    source_string += '\n<!--codebraid.eof-->\n\n'
                else:
                    source_string += '\n\n<!--codebraid.eof-->\n\n'
                self.sources[source_name] = source_string
            self.concat_source_string = ''.join(self.sources.values())

        self.from_format_pandoc_extensions = from_format_pandoc_extensions

        if not isinstance(scroll_sync, bool):
            raise TypeError
        self.scroll_sync = scroll_sync
        if scroll_sync:
            self._io_map = True
            raise NotImplementedError

        self._asts = {}
        self._para_plain_source_name_node_line_number = []
        self._final_ast = None


    from_formats = set(['markdown'])
    multi_source_formats = set(['markdown'])
    to_formats = None


    def _split_format_extensions(self, format_extensions):
        '''
        Split a Pandoc `--from` or `--to` string of the form
        `format+extension1-extension2` into the format and extensions.
        '''
        if format_extensions is None:
            return (None, None)
        index = min([x for x in (format_extensions.find('+'), format_extensions.find('-')) if x >= 0], default=-1)
        if index >= 0:
            return (format_extensions[:index], format_extensions[index:])
        return (format_extensions, None)


    # Node sets are based on pandocfilters
    # https://github.com/jgm/pandocfilters/blob/master/pandocfilters.py
    _null_block_node_type = set([''])
    _block_node_types = set(['Plain', 'Para', 'CodeBlock', 'RawBlock',
                             'BlockQuote', 'OrderedList', 'BulletList',
                             'DefinitionList', 'Header', 'HorizontalRule',
                             'Table', 'Div']) | _null_block_node_type


    def _run_pandoc(self, *,
                    from_format: str,
                    to_format: Optional[str],
                    from_format_pandoc_extensions: Optional[str]=None,
                    to_format_pandoc_extensions: Optional[str]=None,
                    file_scope=False,
                    input: Optional[Union[str, bytes]]=None,
                    input_paths: Optional[Union[pathlib.Path, Sequence[pathlib.Path]]]=None,
                    input_name: Optional[str]=None,
                    output_path: Optional[pathlib.Path]=None,
                    standalone: bool=False,
                    newline_lf: bool=False,
                    preserve_tabs: bool=False,
                    other_pandoc_args: Optional[List[str]]=None):
        '''
        Convert between formats using Pandoc.

        Communication with Pandoc is accomplished via pipes.
        '''
        if from_format_pandoc_extensions is None:
            from_format_pandoc_extensions = ''
        if to_format_pandoc_extensions is None:
            to_format_pandoc_extensions = ''
        if input and input_paths:
            raise TypeError
        cmd_list = [str(self.pandoc_path),
                    '--from', from_format + from_format_pandoc_extensions]
        if newline_lf:
            cmd_list.extend(['--eol', 'lf'])
        if to_format is not None:
            cmd_list.extend(['--to', to_format + to_format_pandoc_extensions])
        if standalone:
            cmd_list.append('--standalone')
            if to_format is None:
                if output_path is not None and output_path.suffix in ('.md', '.markdown'):
                    cmd_list.extend(['--template', self.codebraid_markdown_roundtrip_template_path.as_posix()])
            elif to_format == 'markdown':
                cmd_list.extend(['--template', self.codebraid_markdown_roundtrip_template_path.as_posix()])
        if file_scope:
            cmd_list.append('--file-scope')
        if preserve_tabs:
            cmd_list.append('--preserve-tabs')
        if output_path:
            cmd_list.extend(['--output', output_path.as_posix()])
        if other_pandoc_args:
            if not newline_lf:
                cmd_list.extend(other_pandoc_args)
            else:
                eol = False
                for arg in other_pandoc_args:
                    if arg == '--eol':
                        eol = True
                        continue
                    if eol:
                        eol = False
                        continue
                    cmd_list.append(arg)
        if input_paths is not None:
            if isinstance(input_paths, pathlib.Path):
                cmd_list.append(input_paths.as_posix())
            else:
                cmd_list.extend([p.as_posix() for p in input_paths])

        if platform.system() == 'Windows':
            # Prevent console from appearing for an instant
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        else:
            startupinfo = None

        if isinstance(input, str):
            input = input.encode('utf8')

        try:
            proc = subprocess.run(cmd_list,
                                  input=input,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  startupinfo=startupinfo, check=True)
        except subprocess.CalledProcessError as e:
            if input_paths is not None and input_name is None:
                if isinstance(input_paths, pathlib.Path):
                    input_name = "{0}".format(input_paths.as_posix())
                else:
                    input_name = ', '.join("{0}".format(p.as_posix()) for p in input_paths)
            if input_name is None:
                message = 'Failed to run Pandoc:\n{0}'.format(e.stderr.decode('utf8'))
            else:
                message = 'Failed to run Pandoc on source(s) {0}:\n{1}'.format(input_name, e.stderr.decode('utf8'))
            raise PandocError(message)
        return (proc.stdout, proc.stderr)


    _walk_node_list = staticmethod(walk_node_list)

    def _walk_ast(self, ast, type_filter=None, skip_note_contents=False):
        '''
        Walk all nodes in AST.
        '''
        ast_root_node_list = ast['blocks']
        yield from self._walk_node_list(ast_root_node_list, ast, type_filter=type_filter, skip_note_contents=skip_note_contents)


    @staticmethod
    def _io_map_span_node(source_name, line_number):
        '''
        Create an empty span node containing source name and line number as
        attributes.  This is used to attach source info to an AST location,
        and then track that location through AST transformations and
        conversions.
        '''
        span_node = {'t': 'Span',
                     'c': [
                              [
                                  '',  # id
                                  ['codebraid--temp'],  # classes
                                  [['trace', '{0}:{1}'.format(source_name, line_number)]]  # kv pairs
                              ],
                              []  # contents
                          ]
                    }
        return span_node

    @staticmethod
    def _io_map_span_node_to_raw_tracker(span_node):
        span_node['t'] = 'RawInline'
        span_node['c'] = [None, '\x02CodebraidTrace({0})\x03'.format(span_node['c'][0][2][0][1])]


    @staticmethod
    def _freeze_raw_node(node, source_name, line_number,
                         type_translation_dict={'RawBlock': 'CodeBlock', 'RawInline': 'Code'}):
        '''
        Convert a raw node into a special code node.  This prevents the raw
        node from being prematurely interpreted/discarded during intermediate
        AST transformations.
        '''
        node['t'] = type_translation_dict[node['t']]
        raw_format, raw_content = node['c']
        node['c'] = [
                        [
                            '',  # id
                            ['codebraid--temp'],  # classes
                            [['format', raw_format]],  # kv pairs
                        ],
                        raw_content
                    ]

    @staticmethod
    def _freeze_raw_node_io_map(node, source_name, line_number,
                                type_translation_dict={'RawBlock': 'CodeBlock', 'RawInline': 'Code'}):
        '''
        Same as `_freeze_raw_node()`, but also store trace info.
        '''
        node['t'] = type_translation_dict[node['t']]
        raw_format, raw_content = node['c']
        node['c'] = [
                        [
                            '',  # id
                            ['codebraid--temp'],  # classes
                            [['format', raw_format], ['trace', '{0}:{1}'.format(source_name, line_number)]]  # kv pairs
                        ],
                        raw_content
                    ]

    @staticmethod
    def _thaw_raw_node(node,
                       type_translation_dict={'CodeBlock': 'RawBlock', 'Code': 'RawInline'}):
        '''
        Convert a special code node back into its original form as a raw node.
        '''
        node['t'] = type_translation_dict[node['t']]
        node['c'] = [node['c'][0][2][0][1], node['c'][1]]

    @staticmethod
    def _thaw_raw_node_io_map(node,
                              type_translation_dict={'CodeBlock': 'RawBlock', 'Code': 'RawInline'}):
        '''
        Same as `_thaw_raw_node()`, but also return trace info.
        '''
        node['t'] = type_translation_dict[node['t']]
        for k, v in node['c'][0][2]:
            if k == 'format':
                raw_format = v
            elif k == 'trace':
                trace = v
            else:
                raise ValueError
        node['c'] = [raw_format, '\x02CodebraidTrace({0})\x03'.format(trace) + node['c'][1]]


    @staticmethod
    def _find_cb_command(
            text: str,
            *,
            max_command_len=max(len(x) for x in CodeChunk.commands),
            commands_set=CodeChunk.commands,
            before_attr_chars=set('{ \t\n'),
            after_attr_chars=set('} \t\n')
        ) -> List[Tuple[int, int, bool, Optional[str]]]:
        '''
        Find all occurrences of `.cb.` in a string, which may indicate the
        presence of Codebraid code.  Try to extract a command `.cb.<command>`
        for each occurrence, and if a valid command is not found use None.
        Check the characters immediately before/after `.cb.<command>` to
        determine whether this may be part of code attributes.

        Return a list of tuples.  Each tuple contains data about an occurrence
        of `.cb.`:
            (<index>, <line_number>, <maybe_codebraid_attr>, <command>)
        '''
        results = []
        line_num = 1
        start_index = 0
        while True:
            maybe_start_cb_command_index = text.find('.cb.', start_index)
            if maybe_start_cb_command_index == -1:
                break
            line_num += text.count('\n', start_index, maybe_start_cb_command_index)
            maybe_end_cb_command_index = maybe_start_cb_command_index + 4
            for _ in range(max_command_len + 1):
                if text[maybe_end_cb_command_index:maybe_end_cb_command_index+1] in after_attr_chars:
                    break
                maybe_end_cb_command_index += 1
            else:
                maybe_end_cb_command_index = -1
            if maybe_end_cb_command_index == -1:
                command = None
            else:
                command = text[maybe_start_cb_command_index+4:maybe_end_cb_command_index]
                if command not in commands_set:
                    command = None
            if command is None:
                start_index = maybe_start_cb_command_index + 4
            else:
                start_index = maybe_end_cb_command_index
            if maybe_start_cb_command_index < 4 or text[maybe_start_cb_command_index-1] not in before_attr_chars:
                # For Codebraid attr, `.cb.<command>` must always have at
                # least 4 characters in front of it, either something like
                # "`x`{.cb.command}" for inline or "```{.cb.command}" for
                # block.  There must always be a "{" or whitespace in front of
                # `.cb.<command>`.
                maybe_codebraid_attr = False
            elif command is not None and text[maybe_end_cb_command_index:maybe_end_cb_command_index+1] not in after_attr_chars:
                # For Codebraid attr with a valid Codebraid command,
                # `.cb.<command>` must be followed by whitespace or a "}".
                maybe_codebraid_attr = False
            else:
                # It would be possible to introduce additional checks here to
                # handle the case of invalid commands that match Pandoc attr
                # syntax, but that would probably introduce significant
                # complexity for marginal benefit.
                maybe_codebraid_attr = True
            results.append((maybe_start_cb_command_index, line_num, maybe_codebraid_attr, command))
        return results


    def _load_and_process_initial_ast(self, *,
                                      source_string, single_source_name=None):
        '''
        Convert source string into a Pandoc AST and perform a number of
        operations on the AST.
          * Assign source line numbers to some nodes.  This is needed later
            for syncing error messages and is also needed for SyncTeX when the
            output is LaTeX.
          * Locate all code nodes for further processing.
          * Convert all raw nodes into specially marked code nodes.  This
            allows them to pass through later Pandoc conversions without
            being lost or being interpreted before the final conversion.
            These special code nodes are converted back into raw nodes in the
            final AST before the final format conversion.
        '''
        # Convert source string to AST with Pandoc.
        # Order of extensions is important: earlier override later.
        from_format_pandoc_extensions = ''.join(['-latex_macros',
                                                 '-smart'])
        if self.from_format_pandoc_extensions is not None:
            from_format_pandoc_extensions += self.from_format_pandoc_extensions

        stdout_bytes, stderr_bytes = self._run_pandoc(input=source_string,
                                                      input_name=single_source_name,
                                                      from_format=self.from_format,
                                                      from_format_pandoc_extensions=from_format_pandoc_extensions,
                                                      to_format='json',
                                                      newline_lf=True,
                                                      preserve_tabs=True)
        if stderr_bytes:
            sys.stderr.buffer.write(stderr_bytes)
        try:
            if sys.version_info < (3, 6):
                ast = json.loads(stdout_bytes.decode('utf8'))
            else:
                ast = json.loads(stdout_bytes)
        except Exception as e:
            raise PandocError('Failed to load AST (incompatible Pandoc version?):\n{0}'.format(e))
        if not (isinstance(ast, dict) and
                'pandoc-api-version' in ast and isinstance(ast['pandoc-api-version'], list) and
                all(isinstance(x, int) for x in ast['pandoc-api-version']) and 'blocks' in ast):
            raise PandocError('Unrecognized AST format (incompatible Pandoc version?)')
        self._asts[single_source_name] = ast


        # Locate all code nodes to find Codebraid code nodes.  Also locate all
        # raw nodes so that they can be "frozen" to avoid changes during
        # intermediate AST manipulations.
        code_raw_node_tuples = list(self._walk_ast(ast, type_filter=pandoc_code_raw_nodes))
        # Create code chunks.  Determine if any are in notes, in which case
        # the order of appearance in the source may be different from that in
        # the AST.
        code_chunks_in_notes = False
        codebraid_node_set = set()
        for node_tuple in code_raw_node_tuples:
            node, parent_node, parent_node_list, parent_node_list_index, in_note = node_tuple
            node_type = node['t']
            if node_type.startswith('Code'):
                if any(c.startswith('cb.') for c in node['c'][0][1]):
                    if in_note:
                        code_chunks_in_notes = True
                    code_chunk = PandocCodeChunk(node, parent_node, parent_node_list, parent_node_list_index)
                    self.code_chunks.append(code_chunk)
                    codebraid_node_set.add(id(node))
        # Locate all occurrences of `.cb.` in source(s), to provide
        # traceback information for source errors
        if single_source_name is not None:
            sources_cb = [(single_source_name, cb_i) for cb_i in self._find_cb_command(source_string)]
            source_names_stack = [single_source_name]
        else:
            sources_cb = [(src_name, cb_i) for (src_name, src_string) in self.sources.items() for cb_i in self._find_cb_command(src_string)]
            source_names_stack = [k for k in self.sources]
        sources_cb.reverse()
        source_names_stack.reverse()
        # Process code and raw nodes:  Attach location info to code nodes and
        # "freeze" raw nodes.  If necessary, also search raw node contents to
        # eliminate false positives for code node location.
        if self._io_map:
            raise NotImplementedError
        else:
            freeze_raw_node = self._freeze_raw_node
        if len(sources_cb) == len(self.code_chunks):
            # All `.cb.` in source(s) are active Codebraid code chunks, but
            # may be out of order due to notes.
            skipped = []
            for chunk in self.code_chunks:
                src_cb_i = sources_cb.pop()
                src_name, (_, src_line_number, _, src_command) = src_cb_i
                if chunk.command != src_command:
                    while chunk.command != src_command:
                        skipped.append(src_cb_i)
                        src_cb_i = sources_cb.pop()
                        src_name, (_, src_line_number, _, src_command) = src_cb_i
                chunk.source_name = src_name
                if chunk.inline:
                    chunk.source_start_line_number = src_line_number
                else:
                    chunk.source_start_line_number = src_line_number + 1
                if skipped:
                    while skipped:
                        sources_cb.append(skipped.pop())
            for node_tuple in code_raw_node_tuples:
                node, parent_node, parent_node_list, parent_node_list_index, in_note = node_tuple
                node_type = node['t']
                if node_type.startswith('Raw'):
                    if single_source_name is None:
                        node_format, node_contents = node['c']
                        node_format = node_format.lower()
                        if node_format == 'html' and node_contents == '<!--codebraid.eof-->':
                            node['t'] = 'Null'
                            del node['c']
                            continue
                    freeze_raw_node(node, '', 0)
        else:
            # Some `.cb.` in source(s) are not active Codebraid code chunks.
            # They are false positives or commented-out Codebraid code chunks.
            # If notes are present, nodes may be out of order.
            code_chunk_iter = iter(self.code_chunks)
            skipped = []
            for node_tuple in code_raw_node_tuples:
                node, parent_node, parent_node_list, parent_node_list_index, in_note = node_tuple
                node_type = node['t']
                if single_source_name is None and node_type.startswith('Raw'):
                    node_format, node_contents = node['c']
                    node_format = node_format.lower()
                    if node_format == 'html' and node_contents == '<!--codebraid.eof-->':
                        node['t'] = 'Null'
                        del node['c']
                        current_source_name = source_names_stack.pop()
                        while sources_cb and sources_cb[-1][0] == current_source_name:
                            sources_cb.pop()
                        continue
                node_cb = self._find_cb_command(node['c'][1])
                if node_cb and not node_type.endswith('Block'):
                    # Inline:  code comes before attr
                    for _, _, node_maybe_codebraid, node_command in node_cb:
                        src_cb_i = sources_cb.pop()
                        _, (_, _, _, src_command) = src_cb_i
                        # Can't compare `maybe_codebraid` because Pandoc
                        # strips leading/trailing whitespace from inline code
                        if node_command != src_command:
                            while node_command != src_command:
                                skipped.append(src_cb_i)
                                src_cb_i = sources_cb.pop()
                                _, (_, _, _, src_command) = src_cb_i
                    if skipped:
                        while skipped:
                            sources_cb.append(skipped.pop())
                if id(node) in codebraid_node_set:
                    chunk = next(code_chunk_iter)
                    src_cb_i = sources_cb.pop()
                    src_name, (_, src_line_number, src_maybe_codebraid, src_command) = src_cb_i
                    if chunk.command != src_command or not src_maybe_codebraid:
                        while chunk.command != src_command or not src_maybe_codebraid:
                            skipped.append(src_cb_i)
                            src_cb_i = sources_cb.pop()
                            src_name, (_, src_line_number, src_maybe_codebraid, src_command) = src_cb_i
                    chunk.source_name = src_name
                    if chunk.inline:
                        chunk.source_start_line_number = src_line_number
                    else:
                        chunk.source_start_line_number = src_line_number + 1
                    if skipped:
                        while skipped:
                            sources_cb.append(skipped.pop())
                if node_cb and node_type.endswith('Block'):
                    # Block:  code comes after attr
                    for _, _, node_maybe_codebraid, node_command in node_cb:
                        src_cb_i = sources_cb.pop()
                        _, (_, _, src_maybe_codebraid, src_command) = src_cb_i
                        if node_command != src_command or node_maybe_codebraid != src_maybe_codebraid:
                            while node_command != src_command or node_maybe_codebraid != src_maybe_codebraid:
                                skipped.append(src_cb_i)
                                src_cb_i = sources_cb.pop()
                                _, (_, _, src_maybe_codebraid, src_command) = src_cb_i
                    if skipped:
                        while skipped:
                            sources_cb.append(skipped.pop())
                if node_type.startswith('Raw'):
                    freeze_raw_node(node, '', 0)


    def _extract_code_chunks(self):
        if self.pandoc_file_scope or len(self.sources) == 1:
            for source_name, source_string in self.sources.items():
                self._load_and_process_initial_ast(source_string=source_string, single_source_name=source_name)
        else:
            self._load_and_process_initial_ast(source_string=self.concat_source_string)


    def _postprocess_code_chunks(self):
        for code_chunk in reversed(self.code_chunks):
            # Substitute code output into AST in reversed order to preserve
            # indices
            code_chunk.update_parent_node()
        if self._io_map:
            # Insert tracking spans if needed
            io_map_span_node = self._io_map_span_node
            for source_name, node, line_number in self._para_plain_source_name_node_line_number:
                if node['t'] in ('Para', 'Plain'):
                    # When `example` is in use, a node can be converted into
                    # a different type
                    node['c'].insert(0, io_map_span_node(source_name, line_number))

        # Convert modified AST to markdown, then back, so that raw output
        # can be reinterpreted as markdown.
        # Order of extensions is important: earlier override later.
        processed_to_format_extensions = ''.join(['-latex_macros',
                                                  '-raw_attribute',
                                                  '-smart'])
        if self.from_format_pandoc_extensions is not None:
            processed_to_format_extensions += self.from_format_pandoc_extensions
        processed_markup = collections.OrderedDict()
        for source_name, ast in self._asts.items():
            markup_bytes, stderr_bytes = self._run_pandoc(input=json.dumps(ast),
                                                          from_format='json',
                                                          to_format='markdown',
                                                          to_format_pandoc_extensions=processed_to_format_extensions,
                                                          standalone=True,
                                                          newline_lf=True,
                                                          preserve_tabs=True)
            if stderr_bytes:
                sys.stderr.buffer.write(stderr_bytes)
            processed_markup[source_name] = markup_bytes

        if not self.pandoc_file_scope or len(self.sources) == 1:
            for markup_bytes in processed_markup.values():
                final_ast_bytes, stderr_bytes = self._run_pandoc(input=markup_bytes,
                                                                 from_format='markdown',
                                                                 from_format_pandoc_extensions=self.from_format_pandoc_extensions,
                                                                 to_format='json',
                                                                 newline_lf=True,
                                                                 preserve_tabs=True)
                if stderr_bytes:
                    sys.stderr.buffer.write(stderr_bytes)
        else:
            with tempfile.TemporaryDirectory() as tempdir:
                tempdir_path = pathlib.Path(tempdir)
                tempfile_paths = []
                random_suffix = util.random_ascii_lower_alpha(16)
                for n, markup_bytes in enumerate(processed_markup.values()):
                    tempfile_path = tempdir_path / 'codebraid_intermediate_{0}_{1}.txt'.format(n, random_suffix)
                    tempfile_path.write_bytes(markup_bytes)
                    tempfile_paths.append(tempfile_path)
                final_ast_bytes, stderr_bytes = self._run_pandoc(input_paths=tempfile_paths,
                                                                 from_format='markdown',
                                                                 from_format_pandoc_extensions=self.from_format_pandoc_extensions,
                                                                 to_format='json',
                                                                 newline_lf=True,
                                                                 preserve_tabs=True)
                if stderr_bytes:
                    sys.stderr.buffer.write(stderr_bytes)
        if sys.version_info < (3, 6):
            final_ast = json.loads(final_ast_bytes.decode('utf8'))
        else:
            final_ast = json.loads(final_ast_bytes)
        self._final_ast = final_ast

        if not self._io_map:
            thaw_raw_node = self._thaw_raw_node
            for node_tuple in self._walk_ast(final_ast):
                node, parent_node, parent_node_list, parent_node_list_index, in_note = node_tuple
                node_type = node['t']
                if node_type in ('Code', 'CodeBlock') and 'codebraid--temp' in node['c'][0][1]:
                    thaw_raw_node(node)
        else:
            io_tracker_nodes = []
            io_map_span_node_to_raw_tracker = self._io_map_span_node_to_raw_tracker
            thaw_raw_node = self._thaw_raw_node_io_map
            for node_tuple in self._walk_ast(final_ast):
                node, parent_node_list, parent_node_list_index = node_tuple
                node_type = node['t']
                if node_type == 'Span' and 'codebraid--temp' in node['c'][0][1]:
                    io_map_span_node_to_raw_tracker(node)
                    io_tracker_nodes.append(node)
                if node_type in ('Code', 'CodeBlock') and 'codebraid--temp' in node['c'][0][1]:
                    thaw_raw_node(node)
            self._io_tracker_nodes = io_tracker_nodes


    def convert(self, *, to_format, output_path=None, overwrite=False,
                standalone=None, other_pandoc_args=None):
        if to_format is None:
            if output_path is None:
                raise ValueError('Explicit output format is required when it cannot be inferred from output file name')
            to_format_pandoc_extensions = None
        else:
            if not isinstance(to_format, str):
                raise TypeError
            to_format, to_format_pandoc_extensions = self._split_format_extensions(to_format)
        if not isinstance(standalone, bool):
            raise TypeError
        if output_path is not None:
            if not isinstance(output_path, pathlib.Path):
                if isinstance(output_path, str):
                    output_path = pathlib.Path(output_path)
                else:
                    raise TypeError

        if not isinstance(overwrite, bool):
            raise TypeError
        if output_path is not None and not overwrite and output_path.is_file():
            raise RuntimeError('Output path "{0}" exists, but overwrite=False'.format(output_path))

        if not self._io_map:
            converted_bytes, stderr_bytes = self._run_pandoc(input=json.dumps(self._final_ast),
                                                             from_format='json',
                                                             to_format=to_format,
                                                             to_format_pandoc_extensions=to_format_pandoc_extensions,
                                                             standalone=standalone,
                                                             output_path=output_path,
                                                             other_pandoc_args=other_pandoc_args,
                                                             preserve_tabs=True)
            if stderr_bytes:
                sys.stderr.buffer.write(stderr_bytes)
            if output_path is None:
                sys.stdout.buffer.write(converted_bytes)
        else:
            for node in self._io_tracker_nodes:
                node['c'][0] = to_format
            converted_bytes, stderr_bytes = self._run_pandoc(input=json.dumps(self._final_ast),
                                                             from_format='json',
                                                             to_format=to_format,
                                                             to_format_pandoc_extensions=to_format_pandoc_extensions,
                                                             standalone=standalone,
                                                             newline_lf=True,
                                                             other_pandoc_args=other_pandoc_args,
                                                             preserve_tabs=True)
            if stderr_bytes:
                sys.stderr.buffer.write(stderr_bytes)
            converted_lines = util.splitlines_lf(converted_bytes.decode(encoding='utf8')) or ['']
            converted_to_source_dict = {}
            trace_re = re.compile(r'\x02CodebraidTrace\(.+?:\d+\)\x03')
            for index, line in enumerate(converted_lines):
                if '\x02' in line:
                    #  Tracking format:  '\x02CodebraidTrace({0})\x03'
                    line_split = line.split('\x02CodebraidTrace(', 1)
                    if len(line_split) == 1:
                        continue
                    line_before, trace_and_line_after = line_split
                    trace, line_after = trace_and_line_after.split(')\x03', 1)
                    line = line_before + line_after
                    converted_to_source_dict[str(index + 1)] = trace
                    if '\x02' in line:
                        line = trace_re.sub('', line)
                    converted_lines[index] = line
            converted_lines[-1] = converted_lines[-1] + '\n'
            converted = '\n'.join(converted_lines)
            if self.synctex:
                self._save_synctex_data(converted_to_source_dict)
            if output_path is not None:
                output_path.write_text(converted, encoding='utf8')
            else:
                sys.stdout.buffer.write(converted.encode('utf8'))
