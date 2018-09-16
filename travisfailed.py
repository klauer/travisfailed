'''
Usage:
    travisfailed.py <build_url> [--test-prefix=<prefix>] [--diff] [--no-count]
                        [--diff-tool=<diff_tool>] [--save-path=<save_path>]
                        [--max-diff=<max-diff>] [--verbose] [--skipped]

Options:
    --test-prefix=<prefix>   Relative path prefix shown in py.test
                             verbose output [default: caproto/tests]
    --diff                   Perform diff of results
    --no-count               Do not count failed results from different jobs
    --diff-tool=<tool>       Use this diff tool [default: vimdiff]
    --max-diff=<max-diff>    Maximum number of files to diff
    --verbose                Increase verbosity [default: True]
    --save-path=<save_path>  Save logs to <save_path> [default: build_logs]
    --skipped                Include skipped test in list
'''

import subprocess
import json
import sys
import os
import io
import re
import tempfile
from collections import Counter

import docopt


def travis_request(url, *, as_json=True):
    'Use travis.rb to send a request'
    raw = subprocess.check_output(['travis', 'raw', '--json', url])
    if as_json:
        return json.loads(raw, encoding='utf-8')
    else:
        return raw.decode('utf-8')


def get_jobs(url):
    'Get all jobs, given build url'
    res = travis_request(url)
    return {job['id']: job
            for job in res['jobs']
            }


def get_log(job_id):
    'Get a log of a specific job id'
    url = '/jobs/{}/log'.format(job_id)
    log = travis_request(url)
    if isinstance(log, dict):
        log = log['log']['body']
    with io.StringIO(log) as f:
        lines = [line.strip() for line in f.readlines()]
    return lines


def get_job_desc(job):
    return '{id} py{python} {state} {env:.50}'.format(**job, **job['config'])


def grep_log_for_tests(log_lines, test_path, *, markers=None,
                       verbose=False):
    'Grep (verbose) test lines for tests which (e.g.) failed'
    if markers is None:
        markers = ('FAILED', 'ERROR')

    failed_tests = []
    for line in log_lines:
        if any(m in line for m in markers):
            if verbose:
                print(line)
            if line.startswith(test_path):
                test_name = line.split(' ', 1)[0]
                failed_tests.append(test_name)
    return failed_tests


def list_jobs(jobs, *, file=sys.stdout):
    'List a brief description of all jobs'
    print('Jobs', file=file)
    print('----', file=file)
    for id_, job in jobs.items():
        print(get_job_desc(job), file=file)
    print(file=file)


def parse_log(id_, lines):
    'Parse a log, returning a dictionary of failed_test to log lines'
    # TODO this is terrible
    error_marker = '==================================== ERRORS ===================================='
    failure_marker = '=================================== FAILURES ==================================='  # noqa
    test_marker = re.compile('^.*_______+ (.*) __________+.*$')
    end_marker = re.compile('^.*=====+ .* in .* seconds ====+.*$')

    starting_points = []
    for marker in (error_marker, failure_marker):
        try:
            starting_points.append(lines.index(marker))
        except ValueError:
            error_start = None

    if not starting_points:
        print(f'ERROR: failed to parse job {id_}')
        return {}

    lines = lines[min(starting_points) + 1:]

    failed_lines = {}
    current_test = None

    for line in lines:
        m = test_marker.match(line)
        if m:
            current_test = m.groups()[0]
            failed_lines[current_test] = []
            continue
        elif line in (failure_marker, error_marker):
            current_test = None
            continue

        m = end_marker.match(line)
        if m:
            break

        if current_test:
            failed_lines[current_test].append(line)

    # for result, lines in failed_lines.items():
    #     print(result, '\n'.join(lines))
    return failed_lines


def compare_failures_with_tool(jobs, *, diff_tool, diff_tool_args=None,
                               max_diff=None):
    if diff_tool_args is None:
        diff_tool_args = []

    all_keys = set()
    for id_, job_info in jobs.items():
        if 'log' not in job_info:
            job_info['failed_logs'] = {}
            continue

        failed_logs = parse_log(id_, job_info['log'])
        job_info['failed_logs'] = failed_logs
        for key in failed_logs:
            all_keys.add(key)

    for key in all_keys:
        logs = {}
        for id_, job_info in jobs.items():
            failed_logs = job_info['failed_logs']
            try:
                log = '\n'.join(failed_logs[key])
            except KeyError:
                ...
            else:
                if log not in logs.values():
                    logs[id_] = log
                    if max_diff is not None:
                        if len(logs) >= max_diff:
                            break

        if len(set(logs.values())) > 1:
            log_fs = [tempfile.NamedTemporaryFile(
                      suffix=f'{id_}_{key}', mode='wt')
                      for id_ in logs]
            for id_, tf in zip(logs, log_fs):
                tf.write(logs[id_])
                tf.flush()
            log_fns = [f.name for f in log_fs]
            subprocess.call([diff_tool] + diff_tool_args +
                            log_fns
                            )


def main(build_url, *, verbose=False, save_path='build_logs',
         test_prefix='caproto/tests', count_failed=False,
         run_diff=False, diff_tool='vimdiff', max_diff=None,
         skipped=False):

    if 'api.travis-ci.org' not in build_url:
        build_url = build_url.replace('travis-ci.org/',
                                      'api.travis-ci.org/repos/')

    jobs = get_jobs(build_url)

    if verbose:
        list_jobs(jobs)

    failed_tests = Counter()
    skipped_tests = Counter()
    for id_, job in jobs.items():
        if job['state'] not in ('failed', 'errored'):
            continue

        print(get_job_desc(job))
        if verbose:
            print('---------------------------')

        log_fn = '{}.txt'.format(job['id'])
        local_fn = os.path.join(save_path, log_fn)
        if os.path.exists(local_fn):
            with open(local_fn, 'rt') as f:
                log_lines = [line.strip() for line in f.readlines()]
        else:
            log_lines = get_log(job['id'])

            with open(local_fn, 'wt') as f:
                for line in log_lines:
                    print(line, file=f)

        job['log'] = log_lines

        for test in grep_log_for_tests(log_lines, test_prefix,
                                       markers=('FAILED', 'ERROR'),
                                       verbose=verbose):
            failed_tests[test] += 1

        if skipped:
            for test in grep_log_for_tests(log_lines, test_prefix,
                                           markers=('SKIPPED', ),
                                           verbose=verbose):
                skipped_tests[test] += 1

        if verbose:
            print()
            print()
            print()

    if skipped and skipped_tests:
        print()
        print('Skip Count / Tests')
        print('---------------------')
        for test, count in skipped_tests.items():
            print(f'{count} {test}')
        print()

    if count_failed:
        print()
        print('Failure Count / Tests')
        print('---------------------')
        for test, count in sorted(
                sorted(failed_tests.items()),
                key=lambda x: x[1], reverse=True):
            print(f'{count} {test}')
        print()

    if run_diff:
        compare_failures_with_tool(jobs, diff_tool=diff_tool,
                                   max_diff=max_diff)


if __name__ == '__main__':
    parsed = docopt.docopt(__doc__)
    build_url = parsed['<build_url>']
    test_prefix = parsed['--test-prefix']
    diff_tool = parsed['--diff-tool']
    run_diff = parsed['--diff']
    max_diff = int(parsed['--max-diff']) if parsed['--max-diff'] else None
    count_failed = not parsed['--no-count']
    verbose = parsed['--verbose']
    save_path = parsed['--save-path']
    skipped = parsed['--skipped']

    if save_path and not os.path.exists(save_path):
        os.makedirs(save_path, exist_ok=True)

    main(build_url, verbose=verbose, save_path=save_path,
         test_prefix=test_prefix, count_failed=count_failed,
         run_diff=run_diff, diff_tool=diff_tool,
         max_diff=max_diff, skipped=skipped)
