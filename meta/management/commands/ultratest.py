import sys
import tempfile
import subprocess
import djclick as click
import xml.etree.ElementTree as etree
from django.core.management.base import CommandError

from docker_django_management import IS_RUNNING_IN_DOCKER


# This is based on:
#
# https://gist.github.com/danielrehn/d2e6f2129e5f853c3166

RAINBOW_COLORS = [
    "\x1b[38;5;" + color
    for color in [
        "160;01m",
        "196;01m",
        "202;01m",
        "208;01m",
        "214;01m",
        "220;01m",
        "226;01m",
        "190;01m",
        "154;01m",
        "118;01m",
        "046;01m",
        "047;01m",
        "048;01m",
        "049;01m",
        "051;01m",
        "039;01m",
        "027;01m",
        "021;01m",
        "021;01m",
        "057;01m",
        "093;01m",
    ]
]

RESET_COLORS = "\033[m"

ASCII_ART_LOGO = """\
  _    _ _ _          _______        _
 | |  | | | |        |__   __|      | |
 | |  | | | |_ _ __ __ _| | ___  ___| |_
 | |  | | | __| '__/ _` | |/ _ \/ __| __|
 | |__| | | |_| | | (_| | |  __/\__ \ |_
  \____/|_|\__|_|  \__,_|_|\___||___/\__|"""


def print_with_rainbow_colors(ascii_art):
    for color, line in zip(RAINBOW_COLORS, ascii_art.splitlines()):
        print(color + line)
    print(RESET_COLORS)


def get_coverage():
    COVERAGE_FILE = 'coverage.xml'

    try:
        tree = etree.parse(COVERAGE_FILE)
        root = tree.getroot()
        package = root.find('packages/package')
        return float(package.attrib['branch-rate'])
    except Exception as e:
        return None

    return None

TESTTYPES_TO_REPORT_COVERAGE_ON = ['py.test']
ESLINT_CMD = 'npm run failable-eslint'

if IS_RUNNING_IN_DOCKER:
    # Until https://github.com/benmosher/eslint-plugin-import/issues/142
    # is fixed, we need to disable the following rule for Docker support.
    ESLINT_CMD = ('eslint --rule "import/no-unresolved: off" '
                  '--max-warnings 0 .')

TESTTYPES = [
    {
        'name': 'flake8',
        'cmd': 'flake8 --exclude=node_modules,migrations .'
    },
    {
        'name': 'eslint',
        'cmd': ESLINT_CMD
    },
    {
        'name': 'bandit',
        'cmd': 'bandit -r .'
    },
    {
        'name': 'py.test',
        'cmd': 'py.test --cov-report xml --cov-report term --cov'
    },
    {
        'name': 'jest',
        'cmd': 'npm test'
    },
]


def get_testtype_names(testtypes=None, joiner=", "):
    if not testtypes:
        testtypes = TESTTYPES
    return joiner.join([
        t['name'] for t in testtypes
    ])


def get_testtype(name):
    for t in TESTTYPES:
        if t['name'] == name:
            return t
    raise CommandError(
        '"{}" is not a valid test type. Please choose from {}.'.format(
            name,
            get_testtype_names()
        )
    )


@click.command()
@click.pass_verbosity
@click.argument('testtype', nargs=-1,
                metavar='[{}]'.format(get_testtype_names(joiner="|")))
def command(verbosity, testtype):
    '''
    Test and lint everything!

    Optionally specify one or more test names to run only those tests.
    '''
    is_verbose = verbosity > 1

    # This is kind of annoying; 'testtype' is the most readable name for the
    # CLI end-user, but 'testtype_names' is more readable for the actual
    # implementation, so we'll just assign the latter to the former.
    testtype_names = testtype
    del testtype

    def echo(msg, v_level, **kwargs):
        if verbosity < v_level:
            return
        click.secho(msg, **kwargs)

    print_with_rainbow_colors(ASCII_ART_LOGO)

    if testtype_names:
        to_run = [get_testtype(t) for t in testtype_names]
    else:
        to_run = TESTTYPES

    if to_run == TESTTYPES:
        echo('Running ALL THE TESTS', 1)
    else:
        echo('Running {}'.format(get_testtype_names(to_run)), 1)

    report_coverage = False

    for t in to_run:
        if t['name'] in TESTTYPES_TO_REPORT_COVERAGE_ON:
            report_coverage = True

    if report_coverage:
        # get coverage from the last run, before it is overwritten
        coverage_before = get_coverage()

    failures = []
    failure_outputs = []
    exit_code = 0

    try:
        for entry in to_run:
            echo('-> {} '.format(entry['name']), 1, nl=is_verbose)

            echo('Running "{}"'.format(entry['cmd']), 2)

            out = None if is_verbose else tempfile.TemporaryFile()

            result = subprocess.call(
                entry['cmd'], stdout=out, stderr=subprocess.STDOUT, shell=True
            )

            if result is not 0:
                failures.append(entry['name'])
                if out is not None:
                    out.seek(0)
                    failure_outputs.append(out.read())

            if not is_verbose:
                if result is 0:
                    echo('OK', 1, fg='green')
                else:
                    echo('FAIL', 1, fg='red')
    except KeyboardInterrupt:
        echo('Aborting test run.', 0)
        exit_code = 1

    if failure_outputs:
        for failure, output in zip(failures, failure_outputs):
            echo('-' * 78, 0)
            echo('Output from {}:\n'.format(failure), 0, fg='red')
            sys.stdout.buffer.write(output)
            echo('\n', 0)

    if len(failures) > 0:
        echo('Failing tests: {}'.format(', '.join(failures)), 0)
        exit_code = 1

    if report_coverage:
        coverage_after = get_coverage()
        echo('Coverage: {:.2%}'.format(coverage_after), 1,
             nl=(not coverage_before))
        if coverage_before:
            diff = coverage_after - coverage_before
            if diff < 0:
                echo(' ({:.2%} from last run)'.format(diff), 1, fg='red')
            elif diff > 0:
                echo(' (+{:.2%} from last run)'.format(diff), 1, fg='green')
            else:
                echo(' (same as last run)', 1)

    sys.exit(exit_code)
