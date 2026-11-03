import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

source = Path(os.environ.get('CLAUDE_PREFLIGHT_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'preflight.py')))
spec = importlib.util.spec_from_file_location('preflight_authentication_test', source)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'profile'
        self.config.mkdir()
        (self.config / 'settings.json').write_text('{}')
        self.env = {'CLAUDE_CONFIG_DIR': str(self.config), 'HTTPS_PROXY': 'http://127.0.0.1:11020'}
        self.policy = {'real_binary': '/bin/true', 'curl_binary': '/usr/bin/curl',
                       'proxy': self.env['HTTPS_PROXY'], 'expected_ips': ['203.0.113.1'],
                       'allowed_countries': ['US'], 'attempts': 1,
                       'timeout_seconds': 2, 'connect_timeout_seconds': 1,
                       'magpie_url': 'http://127.0.0.1:8080', 'optional_checks': {}}

    def response(self, logged_in=True, code=0, **values):
        return subprocess.CompletedProcess([], code, json.dumps({'loggedIn': logged_in, **values}), '')

    def test_status_is_queried_using_current_directory_and_config(self):
        with patch.object(p.subprocess, 'run', return_value=self.response()) as run:
            self.assertTrue(p.authentication_check(self.policy, [], self.env, self.root)['ok'])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['/bin/true', 'auth', 'status', '--json'])
        self.assertEqual(kwargs['env']['CLAUDE_CONFIG_DIR'], str(self.config))
        self.assertEqual(kwargs['cwd'], self.root)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)

    def test_settings_flags_forwarded_but_prompt_is_not(self):
        argv = ['--settings', '{"env":{}}', '--setting-sources=user', '-p', 'a private prompt']
        with patch.object(p.subprocess, 'run', return_value=self.response()) as run:
            p.authentication_check(self.policy, argv, self.env, self.root)
        args = run.call_args.args[0]
        self.assertEqual(args, ['/bin/true', '--settings', '{"env":{}}', '--setting-sources=user', 'auth', 'status', '--json'])

    def test_unlogged_directory_rejected(self):
        with patch.object(p.subprocess, 'run', return_value=self.response(False, 1)):
            with self.assertRaises(p.CheckFailure) as error:
                p.authentication_check(self.policy, [], self.env, self.root)
        self.assertEqual(error.exception.item['name'], 'authentication')
        self.assertEqual(error.exception.code, 77)

    def test_nonzero_exit_cannot_pass_even_if_logged_in_is_true(self):
        with patch.object(p.subprocess, 'run', return_value=self.response(True, 1)):
            with self.assertRaises(p.CheckFailure):
                p.authentication_check(self.policy, [], self.env, self.root)

    def test_incomplete_or_wrong_status_does_not_pass(self):
        for value in ({}, [], {'loggedIn': 1}, {'loggedIn': 'true'}):
            with self.subTest(value=value), patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(value), '')):
                with self.assertRaises(p.CheckFailure):
                    p.authentication_check(self.policy, [], self.env, self.root)

    def test_invalid_json_and_timeout_do_not_pass(self):
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'not json', 'private details')):
            with self.assertRaises(p.CheckFailure):
                p.authentication_check(self.policy, [], self.env, self.root)
        with patch.object(p.subprocess, 'run', side_effect=subprocess.TimeoutExpired('claude', 10)):
            with self.assertRaises(p.CheckFailure):
                p.authentication_check(self.policy, [], self.env, self.root)

    def test_output_does_not_expose_cli_account_details(self):
        with patch.object(p.subprocess, 'run', return_value=self.response(email='private@example.test', accessToken='secret-test-value')):
            report = p.authentication_check(self.policy, [], self.env, self.root)
        self.assertNotIn('private@example.test', json.dumps(report))
        self.assertNotIn('secret-test-value', json.dumps(report))

    def test_login_status_logout_help_remain_accessible_without_login(self):
        for argv in (['auth', 'login'], ['auth', 'status'], ['auth', 'logout'], ['setup-token'], ['--version'], ['--help']):
            with self.subTest(argv=argv), patch.object(p.subprocess, 'run') as run:
                item = p.authentication_check(self.policy, argv, self.env, self.root)
                self.assertTrue(item['ok'])
                self.assertTrue(item['skipped'])
                run.assert_not_called()

    def test_missing_real_binary_is_reported(self):
        self.policy['real_binary'] = str(self.root / 'missing')
        with self.assertRaises(p.CheckFailure) as error:
            p.authentication_check(self.policy, [], self.env, self.root)
        self.assertEqual(error.exception.item['name'], 'executable')

    def test_unauthenticated_third_party_blocked_even_with_empty_optional_checks(self):
        env = dict(self.env, ANTHROPIC_BASE_URL='https://gateway.example')
        with patch.object(p.subprocess, 'run', return_value=self.response(False, 1)), patch.object(p, 'optional_checks') as optional:
            report = p.run_checks(self.policy, [], env, self.root)
        self.assertFalse(report['ok'])
        self.assertEqual(report['code'], 77)
        self.assertEqual(report['profile'], 'third-party')
        self.assertEqual(report['checks'][-1]['name'], 'authentication')
        optional.assert_not_called()

    def test_third_party_base_does_not_become_official_due_to_saved_account_login(self):
        env = dict(self.env, ANTHROPIC_BASE_URL='https://gateway.example')
        with patch.object(p.subprocess, 'run', return_value=self.response(authMethod='claude.ai')), patch.object(p, 'probe') as official:
            report = p.run_checks(self.policy, [], env, self.root)
        self.assertTrue(report['ok'])
        self.assertEqual(report['profile'], 'third-party')
        self.assertTrue(report['optional_checks_skipped'])
        self.assertEqual([item['name'] for item in report['checks']], ['authentication'])
        official.assert_not_called()

    def test_magpie_has_auth_gate_but_no_gateway_or_proxy_checks_by_default(self):
        env = dict(self.env, ANTHROPIC_BASE_URL=self.policy['magpie_url'], ANTHROPIC_AUTH_TOKEN='fixture-token')
        with patch.object(p.subprocess, 'run', return_value=self.response(authMethod='oauth_token')), \
             patch.object(p, 'magpie_probe') as gateway, patch.object(p.socket, 'create_connection') as proxy:
            report = p.run_checks(self.policy, [], env, self.root)
        self.assertTrue(report['ok'])
        self.assertEqual(report['profile'], 'magpie')
        gateway.assert_not_called()
        proxy.assert_not_called()

    def test_official_network_checks_still_run_and_missing_auth_blocks_afterward(self):
        with patch.object(p.subprocess, 'run', return_value=self.response(False, 1)), \
             patch.object(p, 'probe', return_value={'name': 'fixture', 'ok': True}) as official, \
             patch.object(p.socket, 'create_connection'):
            report = p.run_checks(self.policy, [], self.env, self.root)
        self.assertFalse(report['ok'])
        self.assertEqual(report['profile'], 'claudeai')
        self.assertEqual(official.call_count, 3)
        self.assertEqual(report['checks'][-1]['name'], 'authentication')

    def test_login_command_can_bootstrap_auth_without_prior_credentials(self):
        with patch.object(p.subprocess, 'run') as auth_status, \
             patch.object(p, 'probe', return_value={'name': 'fixture', 'ok': True}) as official, \
             patch.object(p.socket, 'create_connection'):
            report = p.run_checks(self.policy, ['auth', 'login'], self.env, self.root)
        self.assertTrue(report['ok'], report)
        self.assertEqual(official.call_count, 3)
        auth_status.assert_not_called()

    def test_config_directory_cannot_be_redirected_inside_settings(self):
        (self.config / 'settings.json').write_text(json.dumps({'env': {'CLAUDE_CONFIG_DIR': str(self.root / 'other')}}))
        report = p.run_checks(self.policy, [], self.env, self.root)
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'claude-home')


if __name__ == '__main__':
    unittest.main()
