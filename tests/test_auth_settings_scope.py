import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

source = Path(os.environ.get('CLAUDE_PREFLIGHT_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'preflight.py')))
spec = importlib.util.spec_from_file_location('preflight_auth_scope_test', source)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class AuthenticationSettingsScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shared = self.root / '.shared-user'
        self.config = self.shared / '.magpie/home/.claude'
        self.config.mkdir(parents=True)
        self.signin = self.shared / '.magpie/magpie/claude-accounts/signin-case'
        self.signin.mkdir(parents=True)
        self.project = self.root / 'code/project'
        self.project.mkdir(parents=True)
        self.foreign = self.shared / '.claude/settings.json'
        self.foreign.parent.mkdir(parents=True)
        self.foreign.write_text(json.dumps({'env': {
            'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8080',
            'ANTHROPIC_AUTH_TOKEN': 'fixture-magpie-token',
            'HTTPS_PROXY': 'http://127.0.0.1:9999',
        }}))
        self.policy = dict(real_binary='/bin/true', curl_binary='/usr/bin/curl',
                           proxy='http://127.0.0.1:11020', expected_ips=['203.0.113.1'],
                           allowed_countries=['US'], attempts=1,
                           timeout_seconds=2, connect_timeout_seconds=1)
        self.env = {'CLAUDE_CONFIG_DIR': str(self.config), 'HTTPS_PROXY': self.policy['proxy']}
        self.argv = ['auth', 'login', '--claudeai', '--help']

    def run_checks(self, argv=None, cwd=None, env=None):
        with patch.object(p.socket, 'create_connection'), \
             patch.object(p, 'probe', side_effect=lambda policy, name, url, env: {'name': name, 'ok': True}) as probe:
            report = p.run_checks(self.policy, self.argv if argv is None else argv,
                                  self.env if env is None else env,
                                  self.signin if cwd is None else cwd)
        return report, probe.call_count

    def test_login_is_independent_of_unrelated_parent_settings(self):
        for cwd in (self.project, self.signin):
            with self.subTest(cwd=cwd):
                report, probes = self.run_checks(cwd=cwd)
                self.assertTrue(report['ok'], report)
                self.assertEqual(report['profile'], 'claudeai')
                self.assertEqual(probes, 3)

    def test_login_does_not_read_foreign_json_at_all(self):
        self.foreign.write_text('invalid json')
        with patch.object(p, 'read_object', wraps=p.read_object) as read:
            report, probes = self.run_checks()
        self.assertTrue(report['ok'], report)
        self.assertNotIn(self.foreign, [call.args[0] for call in read.call_args_list])

    def test_login_environment_comes_from_selected_user_not_parent(self):
        (self.config / 'settings.json').write_text(json.dumps({'env': {'LANG': 'C.UTF-8'}}))
        env, settings = p.session_configuration(self.policy, self.argv, self.env, self.signin)
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)
        self.assertNotIn('ANTHROPIC_BASE_URL', env)
        self.assertEqual(env['HTTPS_PROXY'], self.policy['proxy'])
        self.assertEqual(env['LANG'], 'C.UTF-8')

    def test_other_auth_management_commands_use_same_scope(self):
        self.foreign.write_text('invalid json')
        for argv in (['auth', 'status'], ['auth', 'logout'], ['setup-token']):
            with self.subTest(argv=argv):
                report, probes = self.run_checks(argv)
                self.assertTrue(report['ok'], report)
                self.assertEqual(probes, 3)

    def test_selected_user_settings_are_still_checked(self):
        for key, value, name in (
                ('NODE_TLS_REJECT_UNAUTHORIZED', '0', 'tls'),
                ('HTTPS_PROXY', 'http://127.0.0.1:9999', 'proxy-env'),
                ('NO_PROXY', '*', 'proxy-bypass')):
            with self.subTest(key=key):
                (self.config / 'settings.json').write_text(json.dumps({'env': {key: value}}))
                report, probes = self.run_checks()
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][-1]['name'], name)
                self.assertEqual(probes, 0)

    def test_inherited_tls_and_proxy_still_checked(self):
        for key, value, name in (
                ('NODE_TLS_REJECT_UNAUTHORIZED', '0', 'tls'),
                ('HTTPS_PROXY', 'http://127.0.0.1:9999', 'proxy-env')):
            with self.subTest(key=key):
                report, probes = self.run_checks(env=dict(self.env, **{key: value}))
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][-1]['name'], name)

    def test_selected_user_can_be_the_shared_profile_itself(self):
        # A config directory explicitly selected by the caller is never excluded.
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.foreign.parent))
        report, probes = self.run_checks(env=env)
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'proxy-env')

    def test_agent_discovery_and_project_protection_remain_enabled(self):
        env, settings = p.session_configuration(self.policy, [], self.env, self.signin)
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture-magpie-token')
        with self.assertRaises(p.CheckFailure) as error:
            p.local_checks(self.policy, [], self.env, self.signin)
        self.assertEqual(error.exception.item['name'], 'project-network')

    def test_agent_session_cannot_use_auth_scope_to_ignore_project_tls(self):
        folder = self.project / '.claude'
        folder.mkdir()
        (folder / 'settings.json').write_text(json.dumps({'env': {'NODE_TLS_REJECT_UNAUTHORIZED': '0'}}))
        for argv in ([], ['-p', 'auth login']):
            with self.subTest(argv=argv):
                report, probes = self.run_checks(argv=argv, cwd=self.project)
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][-1]['name'], 'project-network')

    def test_managed_main_and_fragments_remain_in_scope(self):
        managed = self.root / 'managed'
        fragments = managed / 'managed-settings.d'
        fragments.mkdir(parents=True)
        (managed / 'managed-settings.json').write_text(json.dumps({'env': {'LANG': 'C.UTF-8'}}))
        (fragments / '10-network.json').write_text(json.dumps({'env': {'NODE_TLS_REJECT_UNAUTHORIZED': '0'}}))
        with patch.object(p, 'MANAGED_CONFIG_DIR', managed, create=True):
            report, probes = self.run_checks()
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'tls')
        self.assertEqual(probes, 0)

    def test_managed_login_restriction_is_not_ignored(self):
        managed = self.root / 'managed'
        managed.mkdir()
        (managed / 'managed-settings.json').write_text(json.dumps({'forceLoginMethod': 'gateway'}))
        with patch.object(p, 'MANAGED_CONFIG_DIR', managed, create=True):
            report, probes = self.run_checks()
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'account-mode')


if __name__ == '__main__':
    unittest.main()
