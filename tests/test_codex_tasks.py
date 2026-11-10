"""Codex live command ownership and stored exits."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader(
    'task_status', str(Path(__file__).resolve().parents[1] / 'aitop'))
status = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(status)


def probe(text, code=0, error=''):
    return subprocess.CompletedProcess([], code, text, error)


class CodexProcessTests(unittest.TestCase):
    def test_thread_ownership_helpers_self_and_pid_reuse(self):
        listing = '\n'.join([
            '10 1 Sat Oct 31 12:00:00 2026 /extension/codex',
            '11 10 Sat Oct 31 12:00:10 2026 docker',
            '12 10 Sat Oct 31 12:00:10 2026 bash',
            '13 10 Sat Oct 31 12:00:10 2026 /extension/codex-code-mode-host',
            '14 10 Sat Oct 31 12:00:10 2026 python3',
            '15 10 Sat Oct 31 12:00:10 2026 docker',
            '16 10 Sat Oct 31 12:00:10 2026 docker',
            '17 10 Sat Oct 31 12:00:10 2026 docker',
            '18 11 Sat Oct 31 12:00:10 2026 child',
        ])
        environments = '\n'.join([
            '11 Sat Oct 31 12:00:10 2026 docker --password PRIVATE_VALUE CODEX_THREAD_ID=thread-1',
            '12 Sat Oct 31 12:00:10 2026 bash mcp-server',
            '15 Sat Oct 31 12:00:10 2026 docker CODEX_THREAD_ID=other-thread',
            '16 Sat Oct 31 12:00:11 2026 docker CODEX_THREAD_ID=thread-1',
            '17 Sat Oct 31 12:00:10 2026 docker CODEX_THREAD_ID=thread-1',
        ])
        arguments = '\n'.join([
            '11 Sat Oct 31 12:00:10 2026 docker --password PRIVATE_VALUE',
            '17 Sat Oct 31 12:00:09 2026 docker reused-pid',
        ])
        with patch.object(status.os, 'getpid', return_value=14), \
             patch.object(status.subprocess, 'run', side_effect=[
                 probe(listing), probe(environments), probe(arguments)]) as run:
            rows = status.codex_processes({'thread-1'})
        self.assertEqual([r['os_pid'] for r in rows], [11, 17])
        self.assertEqual([r['title'] for r in rows], ['docker', 'docker'])
        self.assertNotIn('PRIVATE_VALUE', str([r['title'] for r in rows]))
        # argv reaches the details pane; a reused PID gets none and no environment leaks.
        self.assertEqual([r['command'] for r in rows], ['docker --password PRIVATE_VALUE', None])
        self.assertNotIn('CODEX_THREAD_ID', str(rows))
        self.assertNotIn('e', run.call_args_list[2].args[0][1])
        candidates = run.call_args_list[1].args[0][3].split(',')
        self.assertNotIn('13', candidates)
        self.assertNotIn('14', candidates)
        self.assertNotIn('18', candidates)

    def test_missing_ps_permission_failure_and_exited_race(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('ps', 3),
                      subprocess.CalledProcessError(1, 'ps')):
            with self.subTest(error=error), patch.object(status.subprocess, 'run', side_effect=error):
                self.assertIsNone(status.codex_processes({'thread-1'}))
        listing = ('10 1 Sat Oct 31 12:00:00 2026 codex\n'
                   '11 10 Sat Oct 31 12:00:10 2026 docker')
        with patch.object(status.subprocess, 'run', side_effect=[probe(listing), probe('', 1)]):
            self.assertEqual(status.codex_processes({'thread-1'}), [])
        with patch.object(status.subprocess, 'run', side_effect=[probe(listing), probe('', 1, 'denied')]):
            self.assertIsNone(status.codex_processes({'thread-1'}))
        with patch.object(status.subprocess, 'run') as run:
            self.assertEqual(status.codex_processes(set()), [])
            run.assert_not_called()

    @unittest.skipUnless(shutil.which('ps') and Path('/proc').is_dir(), 'requires Linux procps')
    def test_real_command_lives_then_exits_without_a_cleanup_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = Path(directory) / 'codex'
            shutil.copy2('/bin/sh', backend)
            process = subprocess.Popen([str(backend), '-c', 'sleep 1.5 & wait'],
                                       env=dict(os.environ, CODEX_THREAD_ID='isolated-thread'))
            try:
                found = []
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and not found:
                    found = [r for r in status.codex_processes({'isolated-thread'}) or []
                             if r['title'] == 'sleep']
                    time.sleep(0.02)
                self.assertTrue(found)
                self.assertEqual(found[0]['title'], 'sleep')
            finally:
                process.wait(timeout=5)
            self.assertEqual(status.codex_processes({'isolated-thread'}), [])


class CodexCommandHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.threads = [dict(id='thread-1', cwd='/work')]
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')) as db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT,item_id TEXT,item_type TEXT,'
                       'rollout_ordinal INTEGER,started_at_ms INTEGER,completed_at_ms INTEGER,item_json TEXT)')
            for number, (owner, code, state) in enumerate([
                    ('thread-1', 0, 'completed'), ('thread-1', 7, 'failed'),
                    ('thread-1', None, 'inProgress'), ('thread-1', None, 'declined'),
                    ('other-thread', 9, 'failed')]):
                item = dict(status=state, command='/bin/zsh -lc "docker --password PRIVATE_VALUE"',
                            processId=str(number+100), exitCode=code, durationMs=10000,
                            aggregatedOutput='PRIVATE_OUTPUT')
                db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)',
                           (owner, f'exec-{number}', 'commandExecution', number,
                            2000000000000, 2000000010000 if code is not None else None, json.dumps(item)))
            db.commit()

    def test_live_pid_and_logical_id_are_distinct_and_exits_are_preserved(self):
        reader = status.CodexReader(self.root)
        live = dict(thread_id='thread-1', os_pid=321, started=2000000000, title='docker')
        with patch.object(status, 'codex_processes', return_value=[live]):
            rows = reader.tasks(self.threads, 2000000020, True)
        self.assertEqual((rows[0]['id'], rows[0]['os_pid'], rows[0]['process_id'], rows[0]['uptime']),
                         ('pid-321', 321, None, 20))
        stored = {r['id']: r for r in rows[1:]}
        self.assertEqual(set(stored), {'exec-0', 'exec-1', 'exec-2', 'exec-3'})
        self.assertEqual((stored['exec-0']['state'], stored['exec-0']['exit_code']), ('finished', 0))
        self.assertEqual((stored['exec-1']['state'], stored['exec-1']['exit_code']), ('failed', 7))
        self.assertEqual(stored['exec-2']['state'], 'unknown')
        self.assertEqual(stored['exec-3']['state'], 'stopped')
        self.assertEqual(stored['exec-1']['process_id'], '101')
        self.assertIsNone(stored['exec-1']['os_pid'])
        # Arguments reach the details pane only; titles and output stay out.
        self.assertNotIn('PRIVATE_VALUE', str([r['title'] for r in rows]))
        self.assertEqual(stored['exec-1']['command'], 'docker --password PRIVATE_VALUE')
        self.assertNotIn('PRIVATE_OUTPUT', str(rows))
        self.assertEqual(reader.warnings, [])

    def test_live_commands_are_foreground_while_their_call_waits(self):
        rollout = self.root / 'rollout.jsonl'

        def call(call_id, at):
            return dict(type='response_item', timestamp=at,
                        payload=dict(type='custom_tool_call', call_id=call_id, name='exec'))

        def output(call_id, at):
            return dict(type='response_item', timestamp=at, payload=dict(
                type='custom_tool_call_output', call_id=call_id, output='Script running with cell ID 1'))

        # 2000000000 is 2033-05-18T03:33:20Z. The first script yielded; the second is still awaited.
        entries = [call('c1', '2033-05-18T03:33:20Z'), output('c1', '2033-05-18T03:34:00Z'),
                   call('c2', '2033-05-18T03:35:00Z')]
        rollout.write_text('\n'.join(map(json.dumps, entries)))
        threads = [dict(id='thread-1', cwd='/work', rollout_path=str(rollout))]
        live = [dict(thread_id='thread-1', os_pid=1, started=2000000005, title='sleep'),
                dict(thread_id='thread-1', os_pid=2, started=2000000100, title='make')]
        reader = status.CodexReader(self.root)
        with patch.object(status, 'codex_processes', return_value=live):
            rows = reader.tasks(threads, 2000000200)
            self.assertEqual([(r['id'], r['mode']) for r in rows], [('pid-1', 'bg'), ('pid-2', 'fg')])
            rollout.write_text('\n'.join(map(json.dumps, entries + [output('c2', '2033-05-18T03:36:00Z')])))
            rows = reader.tasks(threads, 2000000200)
            self.assertEqual([r['mode'] for r in rows], ['bg', 'bg'])
            # No readable rollout: the kind is unknown, not guessed.
            rows = reader.tasks([dict(id='thread-1', cwd='/work', rollout_path='/missing')], 2000000200)
            self.assertEqual([r['mode'] for r in rows], [None, None])

    def test_stale_in_progress_is_unknown_when_live_inspection_fails(self):
        reader = status.CodexReader(self.root)
        with patch.object(status, 'codex_processes', return_value=None):
            rows = reader.tasks(self.threads, 2100000000)
        self.assertEqual([(r['id'], r['state']) for r in rows], [('exec-2', 'unknown')])
        self.assertEqual(len(reader.warnings), 1)

    def test_schema_changes_do_not_hide_live_tasks_or_crash(self):
        (self.root / 'thread_history_1.sqlite').write_text('not a database')
        reader = status.CodexReader(self.root)
        live = dict(thread_id='thread-1', os_pid=321, started=0, title='docker')
        with patch.object(status, 'codex_processes', return_value=[live]):
            rows = reader.tasks(self.threads, 100, True)
        self.assertEqual(rows[0]['state'], 'running')
        self.assertEqual(len(reader.warnings), 1)

    def test_project_scope_and_guardian_filter_apply_to_task_inspection(self):
        threads = [dict(id='thread-1', cwd='/work', rollout_path='/missing', thread_source='user'),
                   dict(id='thread-2', cwd='/other', rollout_path='/missing', thread_source='user'),
                   dict(id='guardian', cwd='/work', rollout_path='/missing', thread_source='guardian_review')]
        reader = status.CodexReader(self.root)
        with patch.object(reader, 'database', return_value=threads), \
             patch.object(reader, 'history', return_value={}), \
             patch.object(status, 'codex_processes', return_value=[]) as inspect:
            reader.read(Path('/work/subdirectory'), False, 100)
            inspect.assert_called_once_with({'thread-1'})

    def test_program_title_never_copies_arguments_or_assignments(self):
        self.assertEqual(status.command_program('/bin/zsh -lc "curl --header PRIVATE_VALUE"'), 'curl')
        self.assertEqual(status.command_program('TOKEN=PRIVATE_VALUE tool'), '(shell command)')
        self.assertEqual(status.command_program('"unterminated'), '(shell command)')
        self.assertEqual(status.command_program(None), '(shell command)')


if __name__ == '__main__':
    unittest.main()
