#!/usr/bin/env python3
#
# Script to compile and runs tests.
#

import glob
import itertools as it
import math as m
import os
import re
import shutil
import toml

TEST_PATHS = ['tests_']

SUITE_PROLOGUE = """
#include "runners/test_runner.h"
#include <stdio.h>
"""
CASE_PROLOGUE = """
lfs_t lfs;
"""
CASE_EPILOGUE = """
"""

TEST_PREDEFINES = [
    'READ_SIZE',
    'PROG_SIZE',
    'BLOCK_SIZE',
    'BLOCK_COUNT',
    'BLOCK_CYCLES',
    'CACHE_SIZE',
    'LOOKAHEAD_SIZE',
    'ERASE_VALUE',
    'ERASE_CYCLES',
    'BADBLOCK_BEHAVIOR',
]


# TODO
# def testpath(path):
# def testcase(path):
# def testperm(path):

def testsuite(path):
    name = os.path.basename(path)
    if name.endswith('.toml'):
        name = name[:-len('.toml')]
    return name

# TODO move this out in other files
def openio(path, mode='r'):
    if path == '-':
        if 'r' in mode:
            return os.fdopen(os.dup(sys.stdin.fileno()), 'r')
        else:
            return os.fdopen(os.dup(sys.stdout.fileno()), 'w')
    else:
        return open(path, mode)

class TestCase:
    # create a TestCase object from a config
    def __init__(self, config, args={}):
        self.name = config.pop('name')
        self.path = config.pop('path')
        self.suite = config.pop('suite')
        self.lineno = config.pop('lineno', None)
        self.if_ = config.pop('if', None)
        if isinstance(self.if_, bool):
            self.if_ = 'true' if self.if_ else 'false'
        self.if_lineno = config.pop('if_lineno', None)
        self.code = config.pop('code')
        self.code_lineno = config.pop('code_lineno', None)

        self.normal = config.pop('normal',
                config.pop('suite_normal', True))
        self.reentrant = config.pop('reentrant',
                config.pop('suite_reentrant', False))
        self.valgrind = config.pop('valgrind',
                config.pop('suite_valgrind', True))

        # figure out defines and the number of resulting permutations
        self.defines = {}
        for k, v in (
                config.pop('suite_defines', {})
                | config.pop('defines', {})).items():
            if not isinstance(v, list):
                v = [v]

            self.defines[k] = v

        self.permutations = m.prod(len(v) for v in self.defines.values())

        for k in config.keys():
            print('warning: in %s, found unused key %r' % (self.id(), k),
                file=sys.stderr)

    def id(self):
        return '%s#%s' % (self.suite, self.name)


class TestSuite:
    # create a TestSuite object from a toml file
    def __init__(self, path, args={}):
        self.name = testsuite(path)
        self.path = path

        # load toml file and parse test cases
        with open(self.path) as f:
            # load tests
            config = toml.load(f)

            # find line numbers
            f.seek(0)
            case_linenos = []
            if_linenos = []
            code_linenos = []
            for i, line in enumerate(f):
                match = re.match(
                    '(?P<case>\[\s*cases\s*\.\s*(?P<name>\w+)\s*\])' +
                    '|(?P<if>if\s*=)'
                    '|(?P<code>code\s*=)',
                    line)
                if match and match.group('case'):
                    case_linenos.append((i+1, match.group('name')))
                elif match and match.group('if'):
                    if_linenos.append(i+1)
                elif match and match.group('code'):
                    code_linenos.append(i+2)

            # sort in case toml parsing did not retain order
            case_linenos.sort()

            cases = config.pop('cases', [])
            for (lineno, name), (nlineno, _) in it.zip_longest(
                    case_linenos, case_linenos[1:],
                    fillvalue=(float('inf'), None)):
                if_lineno = min(
                    (l for l in if_linenos if l >= lineno and l < nlineno),
                    default=None)
                code_lineno = min(
                    (l for l in code_linenos if l >= lineno and l < nlineno),
                    default=None)
                cases[name]['lineno'] = lineno
                cases[name]['if_lineno'] = if_lineno
                cases[name]['code_lineno'] = code_lineno

            self.if_ = config.pop('if', None)
            if isinstance(self.if_, bool):
                self.if_ = 'true' if self.if_ else 'false'
            self.if_lineno = min(
                (l for l in if_linenos
                    if not case_linenos or l < case_linenos[0][0]),
                default=None)

            self.code = config.pop('code', None)
            self.code_lineno = min(
                (l for l in code_linenos
                    if not case_linenos or l < case_linenos[0][0]),
                default=None)

            # a couple of these we just forward to all cases
            defines = config.pop('defines', {})
            normal = config.pop('normal', True)
            reentrant = config.pop('reentrant', False)
            valgrind = config.pop('valgrind', True)

            self.cases = []
            for name, case in sorted(cases.items(),
                    key=lambda c: c[1].get('lineno')):
                self.cases.append(TestCase(config={
                    'name': name,
                    'path': path + (':%d' % case['lineno']
                        if 'lineno' in case else ''),
                    'suite': self.name,
                    'suite_defines': defines,
                    'suite_normal': normal,
                    'suite_reentrant': reentrant,
                    'suite_valgrind': valgrind,
                    **case}))

            # combine pre-defines and per-case defines
            self.defines = TEST_PREDEFINES + sorted(
                set.union(*(set(case.defines) for case in self.cases)))

            # combine other per-case things
            self.normal = any(case.normal for case in self.cases)
            self.reentrant = any(case.reentrant for case in self.cases)
            self.valgrind = any(case.valgrind for case in self.cases)

        for k in config.keys():
            print('warning: in %s, found unused key %r' % (self.id(), k),
                file=sys.stderr)

    def id(self):
        return self.name
            


def compile(**args):
    # find .toml files
    paths = []
    for path in args['test_paths']:
        if os.path.isdir(path):
            path = path + '/*.toml'

        for path in glob.glob(path):
            paths.append(path)

    if not paths:
        print('no test suites found in %r?' % args['test_paths'])
        sys.exit(-1)

    if not args.get('source'):
        if len(paths) > 1:
            print('more than one test suite for compilation? (%r)'
                % args['test_paths'])
            sys.exit(-1)

        # write out a test suite
        suite = TestSuite(paths[0])
        if 'output' in args:
            with openio(args['output'], 'w') as f:
                # redirect littlefs tracing
                f.write('#define LFS_TRACE_(fmt, ...) do { \\\n')
                f.write(8*' '+'extern FILE *test_trace; \\\n')
                f.write(8*' '+'if (test_trace) { \\\n')
                f.write(12*' '+'fprintf(test_trace, '
                    '"%s:%d:trace: " fmt "%s\\n", \\\n')
                f.write(20*' '+'__FILE__, __LINE__, __VA_ARGS__); \\\n')
                f.write(8*' '+'} \\\n')
                f.write(4*' '+'} while (0)\n')
                f.write('#define LFS_TRACE(...) LFS_TRACE_(__VA_ARGS__, "")\n')
                f.write('#define LFS_TESTBD_TRACE(...) '
                    'LFS_TRACE_(__VA_ARGS__, "")\n')
                f.write('\n')

                f.write('%s\n' % SUITE_PROLOGUE.strip())
                f.write('\n')
                if suite.code is not None:
                    if suite.code_lineno is not None:
                        f.write('#line %d "%s"\n'
                            % (suite.code_lineno, suite.path))
                    f.write(suite.code)
                    f.write('\n')

                for i, define in it.islice(
                        enumerate(suite.defines),
                        len(TEST_PREDEFINES), None):
                    f.write('#define %-24s test_define(%d)\n' % (define, i))
                f.write('\n')

                for case in suite.cases:
                    # create case defines
                    if case.defines:
                        sorted_defines = sorted(case.defines.items())

                        for perm, defines in enumerate(
                                it.product(*(
                                    [(k, v) for v in vs]
                                    for k, vs in sorted_defines))):
                            f.write('const test_define_t '
                                '__test__%s__%s__%d__defines[] = {\n'
                                % (suite.name, case.name, perm))
                            for k, v in defines:
                                f.write(4*' '+'%s,\n' % v)
                            f.write('};\n')
                            f.write('\n')

                        f.write('const test_define_t *const '
                            '__test__%s__%s__defines[] = {\n'
                            % (suite.name, case.name))
                        for perm in range(case.permutations):
                            f.write(4*' '+'__test__%s__%s__%d__defines,\n'
                                % (suite.name, case.name, perm))
                        f.write('};\n')
                        f.write('\n')

                        f.write('const uint8_t '
                            '__test__%s__%s__define_map[] = {\n'
                            % (suite.name, case.name))
                        for k in suite.defines:
                            f.write(4*' '+'%s,\n'
                                % ([k for k, _ in sorted_defines].index(k)
                                    if k in case.defines else '0xff'))
                        f.write('};\n')
                        f.write('\n')

                    # create case filter function
                    if suite.if_ is not None or case.if_ is not None:
                        f.write('bool __test__%s__%s__filter('
                            '__attribute__((unused)) uint32_t perm) {\n'
                            % (suite.name, case.name))
                        if suite.if_ is not None:
                            f.write(4*' '+'#line %d "%s"\n'
                                % (suite.if_lineno, suite.path))
                            f.write(4*' '+'if (!(%s)) {\n' % suite.if_)
                            f.write(8*' '+'return false;\n')
                            f.write(4*' '+'}\n')
                            f.write('\n')
                        if case.if_ is not None:
                            f.write(4*' '+'#line %d "%s"\n'
                                % (case.if_lineno, suite.path))
                            f.write(4*' '+'if (!(%s)) {\n' % case.if_)
                            f.write(8*' '+'return false;\n')
                            f.write(4*' '+'}\n')
                            f.write('\n')
                        f.write(4*' '+'return true;\n')
                        f.write('}\n')
                        f.write('\n')

                    # create case run function
                    f.write('void __test__%s__%s__run('
                        '__attribute__((unused)) struct lfs_config *cfg, '
                        '__attribute__((unused)) uint32_t perm) {\n'
                        % (suite.name, case.name))
                    f.write(4*' '+'%s\n'
                        % CASE_PROLOGUE.strip().replace('\n', '\n'+4*' '))
                    f.write('\n')
                    f.write(4*' '+'// test case %s\n' % case.id())
                    if case.code_lineno is not None:
                        f.write(4*' '+'#line %d "%s"\n'
                            % (case.code_lineno, suite.path))
                    f.write(case.code)
                    f.write('\n')
                    f.write(4*' '+'%s\n'
                        % CASE_EPILOGUE.strip().replace('\n', '\n'+4*' '))
                    f.write('}\n')
                    f.write('\n')

                    # create case struct
                    f.write('const struct test_case __test__%s__%s__case = {\n'
                        % (suite.name, case.name))
                    f.write(4*' '+'.id = "%s",\n' % case.id())
                    f.write(4*' '+'.name = "%s",\n' % case.name)
                    f.write(4*' '+'.path = "%s",\n' % case.path)
                    f.write(4*' '+'.types = %s,\n'
                        % ' | '.join(filter(None, [
                            'TEST_NORMAL' if case.normal else None,
                            'TEST_REENTRANT' if case.reentrant else None,
                            'TEST_VALGRIND' if case.valgrind else None])))
                    f.write(4*' '+'.permutations = %d,\n' % case.permutations)
                    if case.defines:
                        f.write(4*' '+'.defines = __test__%s__%s__defines,\n'
                            % (suite.name, case.name))
                        f.write(4*' '+'.define_map = '
                            '__test__%s__%s__define_map,\n'
                            % (suite.name, case.name))
                    if suite.if_ is not None or case.if_ is not None:
                        f.write(4*' '+'.filter = __test__%s__%s__filter,\n'
                            % (suite.name, case.name))
                    f.write(4*' '+'.run = __test__%s__%s__run,\n'
                        % (suite.name, case.name))
                    f.write('};\n')
                    f.write('\n')

                # create suite define names
                f.write('const char *const __test__%s__define_names[] = {\n'
                    % suite.name)
                for k in suite.defines:
                    f.write(4*' '+'"%s",\n' % k)
                f.write('};\n')
                f.write('\n')

                # create suite struct
                f.write('const struct test_suite __test__%s__suite = {\n'
                    % suite.name)
                f.write(4*' '+'.id = "%s",\n' % suite.id())
                f.write(4*' '+'.name = "%s",\n' % suite.name)
                f.write(4*' '+'.path = "%s",\n' % suite.path)
                f.write(4*' '+'.types = %s,\n'
                    % ' | '.join(filter(None, [
                        'TEST_NORMAL' if suite.normal else None,
                        'TEST_REENTRANT' if suite.reentrant else None,
                        'TEST_VALGRIND' if suite.valgrind else None])))
                f.write(4*' '+'.define_names = __test__%s__define_names,\n'
                    % suite.name)
                f.write(4*' '+'.define_count = %d,\n' % len(suite.defines))
                f.write(4*' '+'.cases = (const struct test_case *const []){\n')
                for case in suite.cases:
                    f.write(8*' '+'&__test__%s__%s__case,\n'
                        % (suite.name, case.name))
                f.write(4*' '+'},\n')
                f.write(4*' '+'.case_count = %d,\n' % len(suite.cases))
                f.write('};\n')
                f.write('\n')

    else:
        # load all suites
        suites = [TestSuite(path) for path in paths]
        suites.sort(key=lambda s: s.name)

        # write out a test source
        if 'output' in args:
            with openio(args['output'], 'w') as f:
                # redirect littlefs tracing
                f.write('#define LFS_TRACE_(fmt, ...) do { \\\n')
                f.write(8*' '+'extern FILE *test_trace; \\\n')
                f.write(8*' '+'if (test_trace) { \\\n')
                f.write(12*' '+'fprintf(test_trace, '
                    '"%s:%d:trace: " fmt "%s\\n", \\\n')
                f.write(20*' '+'__FILE__, __LINE__, __VA_ARGS__); \\\n')
                f.write(8*' '+'} \\\n')
                f.write(4*' '+'} while (0)\n')
                f.write('#define LFS_TRACE(...) LFS_TRACE_(__VA_ARGS__, "")\n')
                f.write('#define LFS_TESTBD_TRACE(...) '
                    'LFS_TRACE_(__VA_ARGS__, "")\n')
                f.write('\n')

                # copy source
                f.write('#line 1 "%s"\n' % args['source'])
                with open(args['source']) as sf:
                    shutil.copyfileobj(sf, f)
                f.write('\n')

                f.write(SUITE_PROLOGUE)
                f.write('\n')

                # add suite info to test_runner.c
                if args['source'] == 'runners/test_runner.c':
                    f.write('\n')
                    for suite in suites:
                        f.write('extern const struct test_suite '
                            '__test__%s__suite;\n' % suite.name)
                    f.write('const struct test_suite *test_suites[] = {\n')
                    for suite in suites:
                        f.write(4*' '+'&__test__%s__suite,\n' % suite.name)
                    f.write('};\n')
                    f.write('const size_t test_suite_count = %d;\n'
                        % len(suites))

def run(**args):
    pass

def main(**args):
    if args.get('compile'):
        compile(**args)
    else:
        run(**args)

if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(
        description="Build and run tests.")
    # TODO document test case/perm specifier
    parser.add_argument('test_paths', nargs='*', default=TEST_PATHS,
        help="Description of test(s) to run. May be a directory, a path, or \
            test identifier. Defaults to all tests in %r." % TEST_PATHS)
    # test flags
    test_parser = parser.add_argument_group('test options')
    # compilation flags
    comp_parser = parser.add_argument_group('compilation options')
    comp_parser.add_argument('-c', '--compile', action='store_true',
        help="Compile a test suite or source file.")
    comp_parser.add_argument('-s', '--source',
        help="Source file to compile, possibly injecting internal tests.")
    comp_parser.add_argument('-o', '--output',
        help="Output file.")
    # TODO apply this to other scripts?
    sys.exit(main(**{k: v
        for k, v in vars(parser.parse_args()).items()
        if v is not None}))
