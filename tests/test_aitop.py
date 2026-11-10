import importlib.machinery
import importlib.util
import json
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader(
    'aitop', str(Path(__file__).resolve().parents[1] / 'aitop'))
spec = importlib.util.spec_from_loader(loader.name, loader)
status = importlib.util.module_from_spec(spec)
loader.exec_module(status)


def bus(*items, warnings=()):
    return subprocess.CompletedProcess([], 0, json.dumps(dict(
        aibus='0.1.0', schema=1, now=2000, items=list(items), warnings=list(warnings))))


class AibusTests(unittest.TestCase):
    def read(self, listing):
        with patch.object(status.shutil, 'which', return_value='/bin/aibus'), \
             patch.object(status.subprocess, 'run', return_value=listing) as call:
            result = status.aibus_rows(2000)
        self.assertEqual(call.call_args.args[0], ['/bin/aibus', 'ls', '--json', '--reader', 'aitop'])
        return result

    def test_missing_aibus_is_silent(self):
        with patch.object(status.shutil, 'which', return_value=None), \
             patch.object(status.subprocess, 'run') as call:
            self.assertEqual(status.aibus_rows(0), ([], []))
        call.assert_not_called()

    def test_entries_become_rows_by_source(self):
        rows, warnings = self.read(bus(
            dict(source='mcp', id='m1', ai='claude', title='wiki MCP', state='running',
                 status='running', pid=10, started=1000, updated=1000),
            dict(source='xverify', id='run-1', ai='codex', state='running', status='running',
                 pid=11, started=1900, updated=1990, progress=[3, 4], now='codex exec',
                 links=dict(report='/tmp/r.md'), host='mac'),
            dict(source='mac', id='slow-log', state='warn', status='late', interval=300,
                 started=0, updated=1000, now='swap 1G'),
            dict(source='mac', id='todo', state='running', status='stopped', pid=12, started=10, updated=10),
            dict(source='xverify', id='run-0', state='finished', status='finished', exit_code=0,
                 started=100, updated=200, now='done'),
            dict(source='task', id='clash', status='ok'),
            dict(source='mcp', title='no id'),
            'not an object',
            warnings=['unreadable: /x.json']))
        by_id = {r['id']: r for r in rows}
        self.assertEqual(sorted(by_id), ['clash', 'm1', 'run-0', 'run-1', 'slow-log', 'todo'])
        self.assertEqual((by_id['m1']['kind'], by_id['m1']['state'], by_id['m1']['uptime']), ('mcp', 'running', 1000))
        self.assertTrue(by_id['m1']['quiet'])
        run = by_id['run-1']
        self.assertEqual((run['ai'], run['bus_ai'], run['progress'], run['progress_label']),
                         ('aibus', 'codex', 75.0, '3/4'))
        self.assertEqual((run['last_line'], run['links'], run['host']), ('codex exec', 'report /tmp/r.md', 'mac'))
        self.assertFalse(run['quiet'])
        self.assertEqual((by_id['slow-log']['state'], by_id['slow-log']['uptime']), ('late', None))
        self.assertEqual(by_id['todo']['state'], 'gone')  # a dead process stays in view
        self.assertEqual((by_id['run-0']['state'], by_id['run-0']['exit_code'], by_id['run-0']['uptime']),
                         ('finished', 0, None))
        self.assertEqual((by_id['clash']['kind'], by_id['clash']['ai'], by_id['clash']['bus_ai']),
                         ('aibus:task', 'aibus', ''))
        # The AI filter goes by the AI the entry reported.
        self.assertEqual([r['id'] for r in status.tui_visible(rows, view(ai='codex'))], ['run-1'])
        self.assertEqual(status.tui_detail(run, 120)[0][0][0].split()[:6], ['id', 'run-1', 'kind', 'xverify', 'ai', 'codex'])
        self.assertEqual(warnings, ['aibus: unreadable: /x.json'])

    def test_failures_are_one_warning_not_a_crash(self):
        for listing in [FileNotFoundError(), subprocess.TimeoutExpired('aibus', 3),
                        subprocess.CalledProcessError(1, 'aibus'),
                        subprocess.CompletedProcess([], 0, 'not json'),
                        subprocess.CompletedProcess([], 0, '[]')]:
            with self.subTest(listing=listing), \
                 patch.object(status.shutil, 'which', return_value='/bin/aibus'), \
                 patch.object(status.subprocess, 'run', side_effect=[listing]
                              if isinstance(listing, Exception) else None, return_value=listing):
                rows, warnings = status.aibus_rows(0)
                self.assertEqual(rows, [])
                self.assertEqual(len(warnings), 1)

    def test_odd_values_are_skipped_not_shown_or_raised(self):
        rows, warnings = self.read(bus(
            dict(source='mcp', id='big', status='running', started=10 ** 400, pid=10 ** 400),
            dict(source='mcp', id='huge', status='running', progress=[10 ** 400, 1]),
            dict(source='mcp', id='bools', now=[], progress=[True, True], exit_code=True, status=True),
            dict(source='mcp', id='esc', status='\x1b[2J\x1b[Hrunning', host='a\x1b[2Jb'),
            dict(source='\x1b[2J', id='bad-source', status='running'),
            dict(source='mcp', id='gone', status='stopped', pid=99, started=1000),
            dict(source='mcp', id='zero', status='running', started=0, updated=0, now='hello'),
            warnings=[]))
        by_id = {r['id']: r for r in rows}
        self.assertEqual(sorted(by_id), ['big', 'bools', 'esc', 'gone', 'huge', 'zero'])
        self.assertEqual((by_id['zero']['uptime'], by_id['zero']['updated']), (2000, 0))  # 0 is a time, not missing
        self.assertIsNone(by_id['big']['uptime'])
        self.assertNotIn('progress', by_id['huge'])
        bools = by_id['bools']
        self.assertEqual((bools['state'], bools['exit_code'], bools.get('progress')), ('unknown', None, None))
        self.assertEqual((by_id['esc']['state'], by_id['esc']['host']), ('[2J[Hrunning', 'a[2Jb'))
        self.assertIsNone(by_id['gone']['uptime'])  # its process ended, so no uptime
        for listing in ['{"items":[],"warnings":null}', '{"items":[],"warnings":{}}']:
            with self.subTest(listing=listing):
                self.assertEqual(self.read(subprocess.CompletedProcess([], 0, listing)), ([], []))

    def test_sources_keep_their_names_and_filters(self):
        rows, _ = self.read(bus(dict(source='task', id='a', status='ok'),
                                dict(source='bus-task', id='b', status='ok'),
                                dict(source='bus-foo', id='c', status='ok'),
                                dict(source='bg-task', id='d', status='running')))
        self.assertEqual(len({r['kind'] for r in rows}), 4)
        lines = status.tui_screen(rows, [], 2000, view(), 120, 30)
        groups = [text(line).strip() for line in lines if line[0][0][1] == 'group']
        self.assertEqual(sorted(groups), ['bg-task', 'bus-foo', 'bus-task', 'task'])
        shown = status.tui_visible(rows, view(kind='bg-task'))
        self.assertEqual([r['id'] for r in shown], ['d'])
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, CLAUDE_CONFIG_DIR=home, CODEX_HOME=home), \
             patch.object(status, 'aibus_rows', return_value=(rows, [])):
            for kind, ids in [('bg-task', ['d']), ('task', ['a']), ('bus-foo', ['c'])]:
                with self.subTest(kind=kind):
                    _, found, _ = status.collect('both', Path('/x'), True, False, True, kind)
                    self.assertEqual([r['id'] for r in found], ids)

    def test_each_source_gets_a_group_after_background_tasks(self):
        rows = [status.row('claude', 'task', 'b1', 'p', 1990, 'running', 'cmd', mode='bg'),
                status.row('codex', 'xverify', 'r1', '', 1990, 'running', 'verify', source='xverify'),
                status.row('-', 'mac', 'slow', '', 1990, 'warn', 'slow-log', source='mac'),
                status.row('-', 'aibus:task', 'c', '', 1990, 'ok', 'clash', source='task'),
                status.row('claude', 'mcp', 'm1', '', 0, 'running', 'wiki', quiet=True, source='mcp'),
                status.row('claude', 'session', 's1', 'p', 1999, 'recent-output', 'work')]
        lines = status.tui_screen(rows, [], 2000, view(), 120, 30)
        groups = [text(line).strip() for line in lines if line[0][0][1] == 'group']
        self.assertEqual(groups, ['MCP', 'Background Tasks', 'mac', 'task', 'xverify', 'Sessions'])
        texts = [text(line) for line in lines]
        self.assertTrue(any(t[2:].startswith('! warn') for t in texts), texts)


class ClaudeSessionTests(unittest.TestCase):
    def test_session_completion_survives_trailing_metadata_and_new_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Path('/work/project')
            logs = root / 'projects/-work-project'
            logs.mkdir(parents=True)
            path = logs / 'session-1.jsonl'
            entries = [dict(type='assistant', cwd=str(project), timestamp=100,
                            message=dict(stop_reason='end_turn')),
                       dict(type='custom-title', customTitle='Example'),
                       dict(type='system', subtype='stop_hook_summary')]
            path.write_text('\n'.join(map(json.dumps, entries)))
            reader = status.ClaudeReader(root)
            rows = reader.read(project, False, 110)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['kind'], rows[0]['state'], rows[0]['title']),
                             ('session', 'turn-complete', 'Example'))
            entries.append(dict(type='user', cwd=str(project), timestamp=111,
                                message=dict(content='Next turn')))
            path.write_text('\n'.join(map(json.dumps, entries)))
            rows = reader.read(project, False, 112)
            self.assertEqual(rows[0]['state'], 'recent-output')
            output = io.StringIO()
            with redirect_stdout(output):
                status.render(rows, False, False, 112)
            self.assertIn('\n\nAI ', output.getvalue())
            self.assertIn('NAME / TITLE', output.getvalue())
            self.assertIn('claude session', output.getvalue())


    def test_session_ended_by_api_error_is_failed_not_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Path('/work/project')
            logs = root / 'projects/-work-project'
            logs.mkdir(parents=True)
            entries = [dict(type='user', cwd=str(project), timestamp=100, message=dict(content='Go')),
                       dict(type='assistant', timestamp=101, isApiErrorMessage=True,
                            message=dict(model='<synthetic>', stop_reason='refusal')),
                       dict(type='system', subtype='api_error')]
            (logs / 'session-1.jsonl').write_text('\n'.join(map(json.dumps, entries)))
            rows = status.ClaudeReader(root).read(project, False, 100000)
            self.assertEqual(rows[0]['state'], 'failed')
            output = io.StringIO()
            with redirect_stdout(output):
                status.render(rows, False, False, 100000)
            self.assertNotIn('claude session', output.getvalue())

class ClaudeAgentTests(unittest.TestCase):
    def agent(self, directory, lines, now=None, meta=None):
        path = Path(directory) / 'agent-a1.jsonl'
        path.write_text(''.join(json.dumps(line) + '\n' for line in lines))
        if meta:
            path.with_suffix('.meta.json').write_text(json.dumps(meta))
        os.utime(path, (1000, 1000))
        return status.ClaudeReader(Path(directory)).agent(path, 'cwd', now or 1100, 'parent')

    def use(self, name, **given):
        return dict(type='assistant', timestamp=950, message=dict(
            content=[dict(type='tool_use', name=name, input=given)]))

    def test_running_agent_counts_from_its_first_record_and_shows_its_latest_call(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.agent(directory, [
                dict(type='user', timestamp=900, message=dict(content='go')),
                self.use('Read', file_path='/a/first.md'),
                self.use('Bash', description='build it', command='make'),
            ])
        self.assertEqual(row['state'], 'recent-output')
        self.assertEqual(row['uptime'], 200)
        self.assertEqual(row['last_line'], 'build it')

    def test_tool_without_a_description_names_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.agent(directory, [self.use('Read', file_path='/a/b/notes.md')])
        self.assertEqual(row['last_line'], 'Read notes.md')

    def test_finished_agent_keeps_its_run_time_and_clears_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.agent(directory, [
                dict(type='user', timestamp=900, message=dict(content='go')),
                self.use('Bash', description='build it'),
            ], now=5000, meta=dict(finished=True))
        self.assertEqual(row['state'], 'finished')
        self.assertEqual(row['uptime'], 100)
        self.assertEqual(row['last_line'], '')

    def test_agent_without_timestamps_has_no_uptime(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.agent(directory, [dict(type='user', message=dict(content='go'))])
        self.assertIsNone(row['uptime'])


class ClaudeTaskTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('lsof'), 'lsof is unavailable')
    def test_live_lsof_does_not_count_a_reader_as_a_running_task(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'job.output'
            output.write_text('test output')
            with output.open('r'):
                with output.open('a'):
                    self.assertEqual(status.held_open([output.resolve()]), {str(output.resolve())})
                self.assertEqual(status.held_open([output.resolve()]), set())

    def test_states_titles_and_live_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/-work-project'
            tasks = root / 'tmp/-work-project/session-1/tasks'
            project.mkdir(parents=True)
            tasks.mkdir(parents=True)
            outputs = {'done': 'ok\n\n[exited with code 0]\n', 'bad': 'x\n[exited with code 2]',
                       'cut': 'remove?\n[killed]\n', 'live': 'step 1\n', 'lost': 'step 1\n',
                       'fg': 'foreground\n'}
            for name, text in outputs.items():
                (tasks / (name + '.output')).write_text(text)
            entries = []
            for name in ['done', 'bad', 'cut', 'live', 'lost']:
                given = dict(command='sleep 9', run_in_background=True)
                if name == 'live':
                    given['description'] = '변환'
                entries += [dict(type='assistant', timestamp=1999999900, message=dict(content=[dict(
                                type='tool_use', id='t-' + name, name='Bash', input=given)])),
                            dict(type='user', message=dict(content=[dict(
                                type='tool_result', tool_use_id='t-' + name,
                                content=f'Command running in background with ID: {name}. Output is ...')]))]
            (project / 'session-1.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in entries))
            live = str((tasks / 'live.output').resolve())
            with patch.object(status, 'TASK_ROOT', root / 'tmp'), \
                 patch.object(status, 'held_open', return_value={live}) as held:
                rows = {r['id']: r for r in status.task_rows(project, 2000000000)}
            self.assertEqual(sorted(held.call_args.args[0]),
                             sorted((tasks / (n + '.output')).resolve() for n in ['fg', 'live', 'lost']))
            self.assertNotIn('fg', rows)
            self.assertEqual({k: r['state'] for k, r in rows.items()},
                             {'done': 'finished', 'bad': 'failed', 'cut': 'stopped',
                              'live': 'running', 'lost': 'stopped'})
            self.assertEqual((rows['live']['title'], rows['done']['title']), ('변환', '(no description)'))
            self.assertEqual(rows['live']['parent'], 'session-1')
            self.assertEqual(rows['live']['uptime'], 100)
            self.assertEqual((rows['done']['exit_code'], rows['bad']['exit_code']), (0, 2))
            self.assertIsNone(rows['cut']['exit_code'])
            output = io.StringIO()
            with redirect_stdout(output):
                status.render(list(rows.values()), False, True, 2000000000)
            self.assertIn('STATE ', output.getvalue())
            self.assertRegex(output.getvalue(), r'failed\(2\)\s+\S+\s+-\s+')
            self.assertRegex(output.getvalue(), r'finished\(0\)\s')
            self.assertRegex(output.getvalue(), r'stopped\s')
            output = io.StringIO()
            with redirect_stdout(output):
                status.render(list(rows.values()), False, False, 2000000000)
            self.assertIn('running', output.getvalue())
            self.assertNotIn('failed', output.getvalue())
            self.assertNotIn('finished', output.getvalue())
            with patch.object(status, 'TASK_ROOT', root / 'tmp'), \
                 patch.object(status, 'held_open', return_value=None):
                self.assertEqual(status.task_rows(project, 0, 'session-1')[3]['state'], 'unknown')

    def test_progress_marks_and_time_left(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/-work-project'
            tasks = root / 'tmp/-work-project/session-1/tasks'
            project.mkdir(parents=True)
            tasks.mkdir(parents=True)
            outputs = {
                'count': '[1/4] a\n[2/4] b\n',
                'bar': 'pulling\r 10%\r 25.5%\r',
                'none': 'track 2/10, 11/04 at 90 % done\n',
                'over': '[5/4] x\n120%\n',
                'ended': '[3/4] c\n[exited with code 1]\n',
                'long': '[7/8] old\n' + 'x' * (status.PROGRESS_SPAN + 10) + '\n',
                'color': '\x1b[32mok\x1b[0m done\n\n',
            }
            for name, text in outputs.items():
                (tasks / (name + '.output')).write_text(text)
            entries = []
            for name in outputs:
                entries += [dict(type='assistant', timestamp=1999999900, message=dict(content=[dict(
                                type='tool_use', id='t-' + name, name='Bash',
                                input=dict(command='sleep 9', run_in_background=True))])),
                            dict(type='user', message=dict(content=[dict(
                                type='tool_result', tool_use_id='t-' + name,
                                content=f'Command running in background with ID: {name}. Output is ...')]))]
            (project / 'session-1.jsonl').write_text('\n'.join(map(json.dumps, entries)))
            live = {str((tasks / (n + '.output')).resolve()) for n in outputs if n != 'ended'}
            with patch.object(status, 'TASK_ROOT', root / 'tmp'), \
                 patch.object(status, 'held_open', return_value=live):
                rows = {r['id']: r for r in status.task_rows(project, 2000000000)}
            self.assertEqual((rows['count']['progress'], rows['count']['progress_label']), (50, '2/4'))
            self.assertEqual(rows['count']['left'], 100)
            self.assertEqual((rows['bar']['progress'], rows['bar']['progress_label']), (25.5, '25.5%'))
            for name in ['none', 'over', 'long']:
                self.assertIsNone(rows[name]['progress'], name)
            self.assertEqual((rows['ended']['state'], rows['ended']['progress'], rows['ended']['left']),
                             ('failed', 75, None))
            output = io.StringIO()
            with redirect_stdout(output):
                status.render(list(rows.values()), True, True, 2000000000)
            self.assertRegex(output.getvalue(), r'UPTIME\s+PROGRESS\s+LEFT\s+NAME')
            self.assertRegex(output.getvalue(), r'running\s+\S+\s+1m40s\s+50%\s+1m40s\s')
            self.assertRegex(output.getvalue(), r'failed\(1\)\s+\S+\s+-\s+75%\s+-\s')
            self.assertIn('progress=2/4', output.getvalue())
            # The last printed line skips blank lines, the end marker and colour codes.
            self.assertEqual(rows['count']['last_line'], '[2/4] b')
            self.assertEqual(rows['bar']['last_line'], '25.5%')
            self.assertEqual(rows['ended']['last_line'], '[3/4] c')
            self.assertEqual(rows['color']['last_line'], 'ok done')

    def test_lsof_distinguishes_writers_readers_and_failed_inspection(self):
        listing = 'p10\nf3\nar\nn/tmp/read-only.output\nf1\naw\nn/tmp/write.output\nf2\nau\nn/tmp/update.output\n'
        with patch.object(status.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 0, listing, '')):
            self.assertEqual(status.held_open([Path('/tmp/write.output')]),
                             {'/tmp/write.output', '/tmp/update.output'})
        for result in [subprocess.CompletedProcess([], 2, '', ''),
                       subprocess.CompletedProcess([], 1, '', 'permission denied')]:
            with self.subTest(result=result), patch.object(status.subprocess, 'run', return_value=result):
                self.assertIsNone(status.held_open([Path('/tmp/write.output')]))
        with patch.object(status.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 1, '', '')):
            self.assertEqual(status.held_open([Path('/tmp/write.output')]), set())
        with patch.object(status.subprocess, 'run', side_effect=FileNotFoundError()):
            self.assertIsNone(status.held_open([Path('/tmp/write.output')]))

    def test_lsof_network_mount_warning_is_not_a_failed_inspection(self):
        warning = ("lsof: WARNING: can't stat() smbfs file system /Volumes/share\n"
                   "      Output information may be incomplete.\n")
        with patch.object(status.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, 'p10\nf1\naw\nn/tmp/write.output\n', warning)) as call:
            self.assertEqual(status.held_open([Path('/tmp/write.output')]), {'/tmp/write.output'})
        self.assertIn('-w', call.call_args.args[0])

    def test_task_description_does_not_fall_back_to_command_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'session.jsonl'
            entries = [dict(type='assistant', timestamp=100, message=dict(content=[dict(
                           type='tool_use', id='t1', name='Bash',
                           input=dict(command='tool --password PRIVATE_VALUE', run_in_background=True))])),
                       dict(type='user', message=dict(content=[dict(
                           type='tool_result', tool_use_id='t1',
                           content='Command running in background with ID: job1. Output is ...')]))]
            transcript.write_text('\n'.join(map(json.dumps, entries)))
            detail = status.task_details(transcript)[0]['job1']
            # The command is kept for the details pane only; the title never uses it.
            self.assertEqual(detail, {'title': '(no description)', 'started': 100,
                                      'command': 'tool --password PRIVATE_VALUE'})

    def test_task_ids_come_from_the_result_field_or_either_message(self):
        def call(name, text, field=None):
            entries = [dict(type='assistant', timestamp=100, message=dict(content=[dict(
                           type='tool_use', id='t-' + name, name='Bash', input=dict(description=name))])),
                       dict(type='user', message=dict(content=[dict(
                           type='tool_result', tool_use_id='t-' + name, content=text)]))]
            if field is not None:
                entries[1]['toolUseResult'] = field
            return entries

        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'session.jsonl'
            entries = (call('started', 'Command running in background with ID: job1. Output is ...')
                       + call('timeout', 'partial\nCommand did not complete within its 600s timeout '
                                         'and was moved to the background (ID: job2). Output is ...')
                       + call('field', 'Command running ...', dict(stdout='', backgroundTaskId='job3'))
                       # A foreground command that prints the message text is not a task.
                       + call('quote', 'log: moved to the background (ID: job4)', dict(stdout='...')))
            transcript.write_text('\n'.join(map(json.dumps, entries)))
            titles = status.task_details(transcript)[0]
            self.assertEqual({k: v['title'] for k, v in titles.items()},
                             {'job1': 'started', 'job2': 'timeout', 'job3': 'field'})

    def test_tasks_older_than_the_tail_are_found_near_their_start(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / 'session.jsonl'
            filler = [dict(type='user', timestamp=t, message=dict(content='x' * 200))
                      for t in range(1000, 1100)]
            task = [dict(type='assistant', timestamp=1050, message=dict(content=[dict(
                        type='tool_use', id='t1', name='Bash', input=dict(description='빌드 대기'))])),
                    dict(type='user', timestamp=1050, toolUseResult=dict(backgroundTaskId='job1'),
                         message=dict(content=[dict(type='tool_result', tool_use_id='t1', content='...')]))]
            entries = filler[:50] + task + filler[50:]
            transcript.write_text('\n'.join(map(json.dumps, entries)))
            with patch.object(status, 'READ_CAP', 600), patch.object(status, 'LOOKUP_SPAN', 2):
                self.assertEqual(status.task_details(transcript)[0], {})
                self.assertEqual(status.older_tasks(transcript, {'job1': 1050, 'other': 1050}),
                                 {'job1': {'title': '빌드 대기', 'command': None, 'started': 1050}})
                # Only the span around the given time is read.
                self.assertEqual(status.older_tasks(transcript, {'job1': 1095}), {})

    def test_subagent_output_links_are_not_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/-work-project'
            tasks = root / 'tmp/-work-project/session-1/tasks'
            agent = project / 'session-1/subagents/agent-a1.jsonl'
            agent.parent.mkdir(parents=True)
            tasks.mkdir(parents=True)
            agent.write_text('{}\n')
            (tasks / 'a1.output').symlink_to(agent)
            with patch.object(status, 'TASK_ROOT', root / 'tmp'), \
                 patch.object(status, 'held_open', return_value=set()):
                self.assertEqual(status.task_rows(project, 0), [])

    def test_foreground_and_background_tasks_are_told_apart(self):
        def use(call, description):
            return dict(type='assistant', timestamp=1999999900, message=dict(content=[dict(
                type='tool_use', id=call, name='Bash', input=dict(description=description))]))

        def answer(call, field):
            return dict(type='user', timestamp=1999999901, toolUseResult=field, message=dict(content=[dict(
                type='tool_result', tool_use_id=call, content='...')]))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/-work-project'
            tasks = root / 'tmp/-work-project/session-1/tasks'
            project.mkdir(parents=True)
            tasks.mkdir(parents=True)
            for name in ('bgjob', 'moved', 'fgjob'):
                (tasks / (name + '.output')).write_text('step 1\n')
            entries = [use('t1', '빌드 대기'), answer('t1', dict(stdout='', backgroundTaskId='bgjob')),
                       use('t2', '긴 테스트'),
                       answer('t2', dict(stdout='', backgroundTaskId='moved', timedOutAfterMs=120000)),
                       use('t3', '끝난 명령'), answer('t3', dict(stdout='done')),
                       # An api-request-blob record repeats calls after their results.
                       dict(type='api-request-blob', message=dict(content=[dict(
                           type='tool_use', id='t3', name='Bash', input=dict(description='끝난 명령'))])),
                       use('t4', '지금 도는 명령')]
            (project / 'session-1.jsonl').write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in entries))
            held = {str((tasks / (n + '.output')).resolve()) for n in ('bgjob', 'moved', 'fgjob')}
            with patch.object(status, 'TASK_ROOT', root / 'tmp'), \
                 patch.object(status, 'held_open', return_value=held):
                rows = {r['id']: r for r in status.task_rows(project, 2000000000)}
            self.assertEqual({k: (r['mode'], r['title']) for k, r in rows.items()},
                             {'bgjob': ('bg', '빌드 대기'), 'moved': ('bg', '긴 테스트 (timeout)'),
                              'fgjob': ('fg', '지금 도는 명령')})
            output = io.StringIO()
            with redirect_stdout(output):
                status.render(list(rows.values()), False, False, 2000000000)
            self.assertIn('fg-task', output.getvalue())
            self.assertIn('bg-task', output.getvalue())

    def test_all_flag_reads_every_project(self):
        seen = []

        def claude(reader, project, all_projects, now):
            seen.append(('claude', all_projects))
            return []

        def codex(reader, project, all_projects, now, include_ended=False):
            seen.append(('codex', all_projects))
            return []

        with patch.object(status.ClaudeReader, 'read', claude), \
             patch.object(status.CodexReader, 'read', codex), \
             patch.object(status, 'aibus_rows', return_value=([], [])), \
             redirect_stdout(io.StringIO()):
            status.main(['-a'])
            status.main(['--ai', 'both'])
        self.assertEqual(seen, [('claude', True), ('codex', True), ('claude', False), ('codex', False)])

    def test_foreground_commands_hide_only_inside_the_read_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/-work-project'
            tasks = root / 'tmp/-work-project/session-1/tasks'
            project.mkdir(parents=True)
            tasks.mkdir(parents=True)
            (tasks / 'old.output').write_text('step 1\n')
            transcript = project / 'session-1.jsonl'
            with patch.object(status, 'TASK_ROOT', root / 'tmp'), \
                 patch.object(status, 'held_open', return_value=set()):
                # No transcript: nothing proves the file is a foreground command.
                self.assertEqual([r['id'] for r in status.task_rows(project, 0)], ['old'])
                self.assertEqual(status.task_details(transcript), ({}, None, []))
                # A tail read that starts after the file was made cannot rule out an
                # older background task.
                entries = [dict(type='user', timestamp=t, message=dict(content='x' * 200))
                           for t in (1000, 4000000000, 4000000001)]
                transcript.write_text('\n'.join(map(json.dumps, entries)))
                with patch.object(status, 'READ_CAP', 600):
                    self.assertEqual(status.task_details(transcript), ({}, 4000000000, []))
                    self.assertEqual([r['id'] for r in status.task_rows(project, 0)], ['old'])
                self.assertEqual(status.task_details(transcript), ({}, 0, []))
                self.assertEqual(status.task_rows(project, 0), [])


class Tty(io.StringIO):
    def isatty(self):
        return True


def view(**changes):
    return dict(dict(ai='both', kind=None, show_all=False, query='', typing=None,
                     selected=None, detail=False, offset=0), **changes)


def text(line):
    return ''.join(t for t, _ in line[0])


class TuiTests(unittest.TestCase):
    def rows(self):
        return [
            status.row('claude', 'task', 'b1', 'p', 1990, 'running', '장르 조회', uptime=100,
                       progress=50, progress_label='2/4', left=100, parent='s1', mode='bg',
                       last_line='[2/4] 장르 조회'),
            status.row('claude', 'task', 'b2', 'p', 1980, 'failed', 'tests', exit_code=2),
            status.row('claude', 'session', 's1', 'p', 1999, 'recent-output', '홈 맥 남은 할일'),
            status.row('codex', 'session', 't1', 'p', 1995, 'in-progress', 'codex work'),
            status.row('claude', 'mcp', 'm1', 'p', 0, 'running', 'claude-wiki-mcp-1', uptime=3600, quiet=True),
        ]

    def test_width_counts_wide_characters_and_fit_pads_or_cuts(self):
        self.assertEqual(status.cell_width('홈 맥'), 5)
        self.assertEqual(status.fit('홈 맥 남은 할일', 6), '홈 맥…')
        self.assertEqual(status.cell_width(status.fit('홈 맥 남은 할일', 6)), 6)
        self.assertEqual(status.fit('ab', 4, '>'), '  ab')
        self.assertEqual(status.fit('anything', 0), '')

    def test_frame_groups_rows_and_keeps_every_line_inside_the_width(self):
        for width in (130, 90):
            with self.subTest(width=width):
                v = view(detail=True)
                lines = status.tui_screen(self.rows(), ['docker down'], 2000, v, width, 40)
                texts = [text(line) for line in lines]
                for t in texts:
                    self.assertLessEqual(status.cell_width(t), width, t)
                rows = [t for t in texts if t[2:4] in ('● ', '○ ', '◐ ', '✗ ')]
                self.assertTrue(all(status.cell_width(t) == width for t in rows), rows)
                groups = [text(line).strip() for line in lines if line[0][0][1] == 'group']
                self.assertEqual(groups, ['MCP', 'Background Tasks', 'Sessions'])
                self.assertEqual(v['selected'], ('claude', 'mcp', 'm1'))
                self.assertTrue(any(lit and t.startswith('▶ ● running') for (_, lit, _), t in zip(lines, texts)))
                # Each row line carries its key so a click can find it.
                self.assertEqual([line[2] for line in lines if line[2]],
                                 [('claude', 'mcp', 'm1'), ('claude', 'task', 'b1'),
                                  ('claude', 'session', 's1'), ('codex', 'session', 't1')])
                self.assertIn('NOW', texts[1])
                self.assertIn('now', texts[texts.index(next(t for t in texts if '홈 맥' in t))])
                self.assertIn('[2/4]', texts[5])
                self.assertIn('LEFT', texts[1])  # a command reports progress
                self.assertIn('2/4', '\n'.join(texts))
                self.assertIn('! docker down', '\n'.join(texts))
                self.assertIn('4 shown, 1 hidden', texts[-1])
                # The key bar stays on the last line even when the list is short.
                self.assertEqual((len(lines), lines[-1][0][0][1]), (40, 'bar'))
                self.assertRegex(texts[1], r'^  STATE +NOW .*UPTIME +LAST +AI ')
                self.assertIn(' AI ', texts[1])  # both AIs are listed, so the AI column shows

    def test_narrow_panes_cut_every_line_to_the_width(self):
        # A wide character that does not fit ends the line; later pieces do not slip in.
        self.assertEqual(status.clip([('가', 'ok'), ('X', 'bad')], 1), [('', 'ok')])
        # The fixed columns alone take 46 cells, so these widths used to overflow.
        for width in (40, 20, 5):
            with self.subTest(width=width):
                lines = status.tui_screen(self.rows(), ['docker down'], 2000, view(detail=True), width, 40)
                self.assertTrue(all(status.cell_width(text(line)) <= width for line in lines))
                self.assertEqual(len(lines), 40)

    def test_hidden_records_and_filters_follow_the_view(self):
        rows = self.rows()
        shown = lambda v: [r['id'] for r in status.tui_visible(rows, v)]
        # MCP containers come first; a background command gets its own group after the foreground ones.
        self.assertEqual(shown(view()), ['m1', 'b1', 's1', 't1'])
        self.assertEqual(shown(view(show_all=True)), ['m1', 'b2', 'b1', 's1', 't1'])
        self.assertEqual(shown(view(ai='codex')), ['t1'])
        self.assertEqual(shown(view(query='할일')), ['s1'])
        lines = status.tui_screen(rows, [], 2000, view(show_all=True), 120, 30)
        self.assertEqual([text(line).strip() for line in lines if line[0][0][1] == 'group'],
                         ['MCP', 'Tasks', 'Background Tasks', 'Sessions'])
        texts = [text(line) for line in lines]
        self.assertTrue(any(t[2:].startswith('✗ failed(2)') for t in texts))
        quiet = [r for r in rows if r['kind'] != 'task']
        self.assertNotIn('LEFT', text(status.tui_screen(quiet, [], 2000, view(), 120, 30)[1]))
        empty = [text(line) for line in status.tui_screen([], [], 2000, view(), 80, 10)]
        self.assertTrue(any('Nothing to show' in t for t in empty))

    def test_keys_move_toggle_and_search(self):
        rows, v = self.rows(), view()
        status.tui_screen(rows, [], 2000, v, 120, 30)
        status.tui_handle(v, 'down', rows)
        self.assertEqual(v['selected'], ('claude', 'task', 'b1'))
        status.tui_handle(v, 'k', rows)
        status.tui_handle(v, 'up', rows)
        self.assertEqual(v['selected'], ('claude', 'mcp', 'm1'))
        status.tui_handle(v, 'enter', rows)
        status.tui_handle(v, 'l', rows)
        self.assertEqual((v['detail'], v['show_all']), (True, True))
        status.tui_handle(v, 'a', rows)
        self.assertEqual(v['ai'], 'claude')
        status.tui_handle(v, '/', rows)
        for key in ['할', '일', 'x', 'backspace', 'q', 'enter']:
            self.assertIsNone(status.tui_handle(v, key, rows))
        self.assertEqual((v['query'], v['typing']), ('할일q', None))
        status.tui_handle(v, 'esc', rows)
        self.assertEqual(v['query'], '')
        self.assertEqual(status.tui_handle(v, 'q', rows), 'quit')

    def test_mouse_clicks_select_and_double_clicks_toggle_details(self):
        rows, v = self.rows(), view()
        status.tui_screen(rows, [], 2000, v, 120, 30)
        session = ('claude', 'session', 's1')
        status.tui_handle(v, ('click', session), rows)
        self.assertEqual((v['selected'], v['detail']), (session, False))
        # Whether ncurses sends a click first or not, one double click opens once.
        status.tui_handle(v, ('double', session), rows)
        self.assertTrue(v['detail'])
        status.tui_handle(v, ('click', ('claude', 'task', 'b1')), rows)
        status.tui_handle(v, ('double', ('claude', 'task', 'b1')), rows)
        self.assertEqual((v['selected'], v['detail']), (('claude', 'task', 'b1'), False))
        status.tui_handle(v, ('click', None), rows)
        self.assertEqual(v['selected'], ('claude', 'task', 'b1'))
        v['typing'] = 'ab'
        status.tui_handle(v, ('click', session), rows)
        self.assertEqual((v['selected'], v['typing']), (('claude', 'task', 'b1'), 'ab'))

    def test_long_lists_scroll_to_the_selected_row(self):
        rows = [status.row('claude', 'task', f'b{i}', 'p', 2000 - i, 'running', f'job {i}') for i in range(30)]
        v = view()
        for _ in range(25):
            status.tui_handle(v, 'down', rows)
        texts = [text(line) for line in status.tui_screen(rows, [], 2000, v, 100, 12)]
        self.assertEqual(len(texts), 12)
        self.assertTrue(any('job 25' in t and t.startswith('▶') for t in texts))

    def test_terminal_opens_the_live_view_and_plain_or_pipes_print_the_table(self):
        with patch.object(status, 'run_tui') as tui, patch.object(status.sys, 'stdin', Tty()), \
             patch.dict(os.environ, {'TERM': 'xterm-256color'}), redirect_stdout(Tty()):
            status.main(['-l', '--ai', 'codex'])
        self.assertEqual((tui.call_args.args[0]['show_all'], tui.call_args.args[0]['ai']), (True, 'codex'))
        for argv, out in ((['--plain'], Tty()), ([], io.StringIO())):
            with self.subTest(argv=argv), patch.object(status, 'run_tui') as tui, \
                 patch.object(status, 'collect', return_value=(0, [], [])), \
                 patch.object(status.sys, 'stdin', Tty()), \
                 patch.dict(os.environ, {'TERM': 'xterm-256color'}), redirect_stdout(out):
                status.main(argv)
            tui.assert_not_called()
            self.assertIn('No ongoing', out.getvalue())


class CommandDetailTests(unittest.TestCase):
    def test_shell_wrappers_open_to_the_script_they_run(self):
        self.assertEqual(status.command_body("/bin/zsh -lc 'cd /w && ls -la'"), 'cd /w && ls -la')
        self.assertEqual(status.command_body('git status'), 'git status')
        self.assertEqual(status.command_body("bash -c 'unclosed"), "bash -c 'unclosed")
        self.assertIsNone(status.command_body(None))
        self.assertIsNone(status.command_body('  '))
        # ps prints argv unquoted with control characters as octal escapes.
        self.assertEqual(status.command_body(r'/bin/zsh -lc cat <<EOF\012한글\011x \d\012EOF', quoted=False),
                         'cat <<EOF\n한글\tx \\d\nEOF')

    def test_command_wraps_and_stops_after_five_lines(self):
        lines = status.tui_command('echo 1\n' + 'x' * 50 + '\n\x1b[31mred\n4\n5\n6', 40)
        self.assertEqual(lines[0], 'command echo 1')
        self.assertTrue(all(status.cell_width(line) <= 39 for line in lines))
        self.assertEqual(len(lines), status.COMMAND_LINES)
        self.assertEqual(lines[-1].strip(), '(+3 more lines)')
        self.assertNotIn('\x1b', ''.join(lines))
        # Wide characters take two columns: 15 of them fill the 31 left after the label.
        self.assertEqual(status.tui_command('한글' * 30, 40)[1].strip(), '글' + '한글' * 7)

    def test_short_screen_keeps_the_key_bar(self):
        r = status.row('claude', 'task', 'job1', '/w', 0, 'running', '빌드', mode='bg',
                       command='\n'.join(f'step {i}' for i in range(9)), last_line='ok')
        view = dict(ai='both', kind=None, show_all=False, query='', typing=None,
                    selected=None, detail=True, offset=0)
        for height in (6, 8, 12):
            lines = status.tui_screen([r], [], 0, view, 80, height)
            self.assertLessEqual(len(lines), height)
            self.assertIn('q quit', ''.join(s for s, _ in lines[-1][0]))
            self.assertTrue(any(key for _, _, key in lines))

    def test_details_show_the_command_before_the_last_output(self):
        r = status.row('claude', 'task', 'job1', '/w', 0, 'running', '빌드', mode='bg',
                       command='make\nmake test', last_line='ok')
        text = [''.join(s for s, _ in line) for line in status.tui_detail(r, 80)]
        start = next(i for i, line in enumerate(text) if line.strip().startswith('command'))
        self.assertEqual([line.strip() for line in text[start:start + 3]],
                         ['command make', 'make test', 'output ok'])


class ReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def counted(self):
        calls, real = [], status.parse

        def parse(data):
            calls.append(len(data))
            return real(data)
        return calls, patch.object(status, 'parse', parse)

    def test_unchanged_file_is_parsed_once(self):
        path = self.dir / 'a.jsonl'
        path.write_text('{"n": 1}\n')
        calls, patched = self.counted()
        with patched:
            first = status.records(path)
            # Within READ_CAP both ends are the same bytes, so they share one read.
            self.assertEqual(status.records(path, tail=True), first)
            self.assertEqual(len(calls), 1)
            with path.open('a') as stream:
                stream.write('{"n": 2}\n')
            self.assertEqual([r['n'] for r in status.records(path)], [1, 2])
        self.assertEqual(len(calls), 2)

    def test_head_of_a_long_file_is_kept_as_it_grows(self):
        path = self.dir / 'a.jsonl'
        line = json.dumps({'pad': 'x' * 1000}) + '\n'
        path.write_text(line * (status.READ_CAP // len(line) + 10))
        calls, patched = self.counted()
        with patched:
            status.records(path)
            status.records(path, tail=True)
            with path.open('a') as stream:
                stream.write('{"n": 2}\n')
            status.records(path)
            self.assertEqual(status.records(path, tail=True)[-1], {'n': 2})
        # The head once, the tail before and after the append.
        self.assertEqual(len(calls), 3)

    def test_older_task_lookup_is_kept(self):
        transcript = self.dir / 's.jsonl'
        transcript.write_text('{"timestamp": "2026-11-09T00:00:00Z"}\n')
        births = {'job1': 1794182400.0}
        calls, patched = self.counted()
        with patched:
            status.older_tasks(transcript, births)
            status.older_tasks(transcript, births)
        self.assertEqual(len(calls), 1)

    def test_collect_drops_what_it_did_not_read(self):
        status.CACHE[('records', str(self.dir / 'gone.jsonl'), False)] = ((0, 0, 0), [])
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.dir)}):
            status.collect('claude', self.dir, False, False, False)
        self.assertNotIn(('records', str(self.dir / 'gone.jsonl'), False), status.CACHE)


if __name__ == '__main__':
    unittest.main()
