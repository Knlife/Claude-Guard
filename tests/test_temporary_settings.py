import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

source = Path(os.environ.get('CLAUDE_PREFLIGHT_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'preflight.py')))
spec = importlib.util.spec_from_file_location('preflight_temporary_test', source)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class TemporarySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / '.claude'
        self.config.mkdir()
        self.project = self.root / 'work'
        self.project.mkdir()
        self.env = {'CLAUDE_CONFIG_DIR': str(self.config), 'HTTPS_PROXY': 'http://127.0.0.1:11020'}
        self.policy = dict(real_binary='/bin/true', curl_binary='/usr/bin/curl',
                           proxy=self.env['HTTPS_PROXY'], expected_ips=['203.0.113.1'],
                           allowed_countries=['US'], attempts=1, timeout_seconds=2,
                           connect_timeout_seconds=1)
        self.managed = self.root / 'managed'
        self.managed.mkdir()
        self.patch = patch.object(p, 'MANAGED_CONFIG_DIR', self.managed)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def flags(self, settings=None):
        return ['--setting-sources', '', '--settings', json.dumps(settings or {})]

    def check(self, argv):
        with patch.object(p.socket, 'create_connection'), \
             patch.object(p, 'probe', side_effect=lambda policy, name, url, env: {'name': name, 'ok': True}) as probe, \
             patch.object(p, 'authentication_check', return_value={'name': 'authentication', 'ok': True}) as auth:
            report = p.run_checks(self.policy, argv, self.env, self.project)
        return report, probe.call_count, auth.call_count

    def test_empty_sources_and_inline_json_support_full_sdk_arguments(self):
        argv = ['-p', '--output-format', 'stream-json', '--input-format', 'stream-json',
                '--include-partial-messages', '--verbose', '--model', 'claude-sonnet-4-5',
                '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                *self.flags({'env': {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}}),
                '--dangerously-skip-permissions', '--no-session-persistence']
        report, probes, auth = self.check(argv)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['profile'], 'claudeai')
        self.assertEqual(probes, 3)
        self.assertEqual(auth, 1)

    def test_disabled_user_project_and_local_files_are_not_read(self):
        user = self.config / 'settings.json'
        user.write_text('invalid user json')
        project = self.project / '.claude'
        project.mkdir()
        (project / 'settings.json').write_text('invalid project json')
        (project / 'settings.local.json').write_text('invalid local json')
        with patch.object(p, 'read_object', wraps=p.read_object) as read:
            report, probes, auth = self.check(self.flags())
        self.assertTrue(report['ok'], report)
        read.assert_not_called()

    def test_empty_sources_does_not_inherit_magpie_routing_from_user_file(self):
        (self.config / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8080', 'ANTHROPIC_AUTH_TOKEN': 'fixture'}}))
        report, probes, auth = self.check(self.flags())
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['profile'], 'claudeai')
        self.assertEqual(probes, 3)

    def test_disabled_user_file_is_not_reintroduced_as_an_ancestor_project(self):
        (self.config / 'settings.json').write_text('invalid disabled user json')
        report, probes, auth = self.check(['--setting-sources', 'project', '--settings', '{}'])
        self.assertTrue(report['ok'], report)

    def test_enabled_file_errors_remain_errors(self):
        (self.config / 'settings.json').write_text('invalid user json')
        report, probes, auth = self.check(['--settings', '{}'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'settings')

    def test_user_only_source_ignores_project_files(self):
        project = self.project / '.claude'
        project.mkdir()
        (project / 'settings.json').write_text('invalid disabled project json')
        report, probes, auth = self.check(['--setting-sources', 'user', '--settings', '{}'])
        self.assertTrue(report['ok'], report)

    def test_inline_and_file_settings_have_same_security_result(self):
        settings = {'env': {'HTTPS_PROXY': self.env['HTTPS_PROXY'], 'LANG': 'C.UTF-8'}}
        path = self.project / 'worker-settings.json'
        path.write_text(json.dumps(settings))
        for argv in (self.flags(settings), ['--setting-sources=', '--settings=worker-settings.json']):
            with self.subTest(argv=argv):
                env, merged = p.session_configuration(self.policy, argv, self.env, self.project)
                checked, checks = p.local_checks(self.policy, argv, self.env, self.project)
                self.assertEqual(env, checked)
                self.assertEqual(env['LANG'], 'C.UTF-8')
                self.assertTrue(all(item['ok'] for item in checks))

    def test_inline_tls_proxy_and_bypass_violations_are_rejected(self):
        for key, value, name in (('NODE_TLS_REJECT_UNAUTHORIZED', '0', 'tls'),
                                 ('HTTPS_PROXY', 'http://127.0.0.1:9', 'proxy-env'),
                                 ('NO_PROXY', '*', 'proxy-bypass')):
            with self.subTest(key=key):
                report, probes, auth = self.check(self.flags({'env': {key: value}}))
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][-1]['name'], name)
                self.assertEqual(probes, 0)
                self.assertEqual(auth, 0)

    def test_missing_proxy_is_not_bypassed_by_temporary_settings(self):
        self.env.pop('HTTPS_PROXY')
        report, probes, auth = self.check(self.flags())
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'proxy-env')

    def test_official_oauth_environment_override_still_rejected(self):
        report, probes, auth = self.check(self.flags({'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'private-fixture-token'}}))
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'account-source')
        self.assertNotIn('private-fixture-token', json.dumps(report))

    def test_temporary_config_directory_override_rejected(self):
        report, probes, auth = self.check(self.flags({'env': {'CLAUDE_CONFIG_DIR': '/another-profile'}}))
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'claude-home')

    def test_invalid_objects_and_env_are_rejected(self):
        for value in ('[]', 'null', 'true', '1', '{"env":[]}', '{"env":{"HTTPS_PROXY":false}}', '{"apiKeyHelper":true}', '{"forceLoginMethod":{}}'):
            with self.subTest(value=value):
                report, probes, auth = self.check(['--setting-sources', '', '--settings', value])
                self.assertFalse(report['ok'])
                self.assertEqual(probes, 0)

    def test_malformed_inline_json_does_not_leak_content(self):
        report, probes, auth = self.check(['--setting-sources', '', '--settings', '{"env":{"ANTHROPIC_API_KEY":"private-fixture-secret"'])
        self.assertFalse(report['ok'])
        self.assertNotIn('private-fixture-secret', json.dumps(report))

    def test_unreadable_or_invalid_settings_file_is_rejected(self):
        for value in ('missing.json', 'bad.json'):
            (self.project / 'bad.json').write_text('invalid json')
            report, probes, auth = self.check(['--setting-sources', '', '--settings', value])
            self.assertFalse(report['ok'])
            self.assertEqual(report['checks'][-1]['name'], 'settings-override')

    def test_managed_policy_still_overrides_cli_with_empty_sources(self):
        (self.managed / 'managed-settings.json').write_text(json.dumps({'env': {'NODE_TLS_REJECT_UNAUTHORIZED': '0'}}))
        report, probes, auth = self.check(self.flags({'env': {'NODE_TLS_REJECT_UNAUTHORIZED': '1'}}))
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'tls')

    def test_managed_fragments_and_login_restrictions_remain_effective(self):
        fragments = self.managed / 'managed-settings.d'
        fragments.mkdir()
        (fragments / '10-auth.json').write_text(json.dumps({'forceLoginMethod': 'gateway'}))
        report, probes, auth = self.check(self.flags({'forceLoginMethod': 'claudeai'}))
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'account-mode')

    def test_enabled_project_override_cannot_be_laundered_by_cli_settings(self):
        project = self.project / '.claude'
        project.mkdir()
        (project / 'settings.json').write_text(json.dumps({'env': {'NODE_TLS_REJECT_UNAUTHORIZED': '0'}}))
        report, probes, auth = self.check(['--settings', '{"env":{"NODE_TLS_REJECT_UNAUTHORIZED":"1"}}'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'project-network')

    def test_unknown_sources_and_missing_values_are_rejected(self):
        for argv in (['--setting-sources', 'unknown'], ['--setting-sources'], ['--settings']):
            with self.subTest(argv=argv):
                report, probes, auth = self.check(argv)
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][-1]['name'], 'settings-override')

    def test_prompt_values_and_end_of_options_are_not_parsed_as_settings(self):
        for argv in (['--system-prompt', '--settings', '--setting-sources=', '--settings={}'],
                     ['--tools', '--settings', '--setting-sources=', '--settings={}']):
            self.assertEqual(p.settings_arguments(argv), ['--setting-sources=', '--settings={}'])
        self.assertEqual(p.settings_arguments(['--', '--settings', 'bad.json']), [])

    def test_last_scalar_override_wins(self):
        argv = ['--setting-sources', 'user', '--setting-sources', '',
                '--settings', '{invalid previous value', '--settings', '{"env":{"LANG":"C.UTF-8"}}']
        env, merged = p.session_configuration(self.policy, argv, self.env, self.project)
        self.assertEqual(env['LANG'], 'C.UTF-8')


if __name__ == '__main__':
    unittest.main()
