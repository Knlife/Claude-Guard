import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

source = Path(os.environ.get('CLAUDE_PREFLIGHT_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'preflight.py')))
spec = importlib.util.spec_from_file_location('preflight_optional_test', source)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class OptionalCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.settings = self.config / 'settings.json'
        self.settings.write_text(json.dumps({'forceLoginMethod': 'claudeai'}))
        self.policy = dict(schema=1, claude_config_dir=str(self.config),
                           real_binary='/bin/true', curl_binary='/usr/bin/curl',
                           proxy='http://127.0.0.1:11020', expected_ips=['203.0.113.1'],
                           allowed_countries=['US'], attempts=1,
                           timeout_seconds=2, connect_timeout_seconds=1,
                           magpie_url='http://127.0.0.1:11025')
        self.env = dict(CLAUDE_CONFIG_DIR=str(self.config), HTTPS_PROXY=self.policy['proxy'])
        self.auth = patch.object(p, 'authentication_check', return_value={'name': 'authentication', 'ok': True, 'message': 'fixture'})
        self.auth_mock = self.auth.start()
        self.addCleanup(self.auth.stop)

    def run_check(self, env=None, argv=()):
        return p.run_checks(self.policy, list(argv), self.env if env is None else env, self.cwd)

    def gateway(self, base=None):
        return dict(self.env, ANTHROPIC_BASE_URL=base or self.policy['magpie_url'],
                    ANTHROPIC_AUTH_TOKEN='fixture-placeholder')

    def test_magpie_skips_every_runtime_check(self):
        env = self.gateway()
        env.update(NODE_TLS_REJECT_UNAUTHORIZED='0', HTTPS_PROXY='http://wrong:9', NO_PROXY='*',
                   CLAUDE_CODE_OAUTH_TOKEN='fixture-oauth', ANTHROPIC_API_KEY='fixture-key')
        with patch.object(p, 'local_checks') as local, patch.object(p, 'magpie_probe') as magpie, \
             patch.object(p, 'probe') as official, patch.object(p.socket, 'create_connection') as conn:
            r = self.run_check(env)
        self.assertTrue(r['ok'], r)
        self.assertTrue(r['optional_checks_skipped'])
        self.assertEqual(r['profile'], 'magpie')
        self.assertEqual([item['name'] for item in r['checks']], ['authentication'])
        self.auth_mock.assert_called_once()
        for mocked in (local, magpie, official, conn):
            mocked.assert_not_called()
        self.assertNotIn('fixture-oauth', json.dumps(r))

    def test_arbitrary_third_party_skips_checks(self):
        r = self.run_check(self.gateway('https://gateway.example'))
        self.assertTrue(r['optional_checks_skipped'])
        self.assertEqual(r['profile'], 'third-party')

    def test_gateway_policy_does_not_force_gateway_mode(self):
        with patch.object(p.socket, 'create_connection'), patch.object(p, 'probe', return_value={'name': 'fixture', 'ok': True}) as probe:
            r = self.run_check()
        self.assertTrue(r['ok'])
        self.assertEqual(r['profile'], 'claudeai')
        self.assertEqual(probe.call_count, 3)

    def test_magpie_can_be_used_on_another_host(self):
        del self.policy['magpie_url']
        r = self.run_check(self.gateway('http://127.0.0.1:11025'))
        self.assertTrue(r['optional_checks_skipped'])

    def test_api_credentials_skip_account_checks(self):
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
            r = self.run_check(dict(self.env, **{key: 'fixture-key'}))
            self.assertTrue(r['optional_checks_skipped'])
            self.assertEqual(r['profile'], 'api')

    def test_cloud_providers_skip_checks(self):
        for provider in ('BEDROCK', 'VERTEX', 'FOUNDRY'):
            r = self.run_check(dict(self.env, **{'CLAUDE_CODE_USE_' + provider: '1'}))
            self.assertTrue(r['optional_checks_skipped'])

    def test_api_helper_and_console_use_api_profile(self):
        for settings in ({'apiKeyHelper': '/example/helper'}, {'forceLoginMethod': 'console'}):
            self.settings.write_text(json.dumps(settings))
            self.assertEqual(self.run_check()['profile'], 'api')

    def test_official_login_without_forced_method_still_runs_strict_checks(self):
        self.settings.write_text('{}')
        with patch.object(p.socket, 'create_connection'), patch.object(p, 'probe', return_value={'name': 'fixture', 'ok': True}) as probe:
            report = self.run_check()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['profile'], 'claudeai')
        self.assertEqual(probe.call_count, 3)
        self.assertFalse(report.get('skipped', False))

    def test_missing_forced_method_does_not_relax_tls_or_proxy_checks(self):
        self.settings.write_text('{}')
        for additions, name in (({'NODE_TLS_REJECT_UNAUTHORIZED': '0'}, 'tls'),
                                ({'HTTPS_PROXY': 'http://wrong:9'}, 'proxy-env'),
                                ({'NO_PROXY': '*'}, 'proxy-bypass')):
            with self.subTest(check=name):
                report = self.run_check(dict(self.env, **additions))
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][-1]['name'], name)

    def test_conflicting_login_restriction_still_rejected(self):
        self.settings.write_text(json.dumps({'forceLoginMethod': 'gateway'}))
        report = self.run_check()
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['name'], 'account-mode')

    def test_official_tls_still_enforced(self):
        r = self.run_check(dict(self.env, NODE_TLS_REJECT_UNAUTHORIZED='0'))
        self.assertFalse(r['ok'])
        self.assertEqual(r['checks'][-1]['name'], 'tls')

    def test_official_proxy_still_enforced(self):
        for additions in ({'HTTPS_PROXY': 'http://wrong:9'}, {'NO_PROXY': '*'}):
            self.assertFalse(self.run_check(dict(self.env, **additions))['ok'])

    def test_official_probe_failure_still_blocks(self):
        with patch.object(p.socket, 'create_connection'), patch.object(p, 'probe', return_value={'name': 'fixture', 'ok': False}):
            self.assertFalse(self.run_check()['ok'])

    def test_oauth_token_does_not_skip_official_checks(self):
        r = self.run_check(dict(self.env, CLAUDE_CODE_OAUTH_TOKEN='fixture-oauth'))
        self.assertEqual(r['profile'], 'claudeai')
        self.assertFalse(r['ok'])

    def test_official_domain_variants_do_not_skip_checks(self):
        for base in ('https://api.anthropic.com', 'https://API.ANTHROPIC.COM.', 'https://claude.ai'):
            self.assertEqual(p.session_profile(self.policy, {'ANTHROPIC_BASE_URL': base}, {}, []), 'claudeai')

    def test_login_commands_keep_official_network_checks(self):
        for argv in (['auth', 'login'], ['setup-token']):
            with patch.object(p.socket, 'create_connection'), patch.object(p, 'probe', return_value={'name': 'fixture', 'ok': True}) as probe:
                r = self.run_check(self.gateway(), argv)
            self.assertEqual(r['profile'], 'claudeai')
            self.assertTrue(r['ok'], r)
            self.assertEqual(probe.call_count, 3)

    def test_opt_in_tls_checks_only_when_selected(self):
        self.policy['optional_checks'] = {'magpie': ['tls']}
        r = self.run_check(dict(self.gateway(), NODE_TLS_REJECT_UNAUTHORIZED='0'))
        self.assertFalse(r['ok'])
        self.assertEqual([x['name'] for x in r['checks']], ['authentication', 'tls'])
        self.assertTrue(self.run_check(self.gateway())['ok'])

    def test_opt_in_gateway_check_only_when_selected(self):
        self.policy['optional_checks'] = {'magpie': ['magpie-gateway']}
        with patch.object(p, 'magpie_probe', return_value={'name': 'magpie-gateway', 'ok': False}) as probe:
            self.assertFalse(self.run_check(self.gateway())['ok'])
        probe.assert_called_once()

    def test_opt_in_checks_do_not_affect_other_profiles(self):
        self.policy['optional_checks'] = {'magpie': ['tls']}
        r = self.run_check(dict(self.gateway('https://gateway.example'), NODE_TLS_REJECT_UNAUTHORIZED='0'))
        self.assertTrue(r['optional_checks_skipped'])

    def test_bad_optional_check_rejected_only_when_selected(self):
        self.policy['optional_checks'] = {'magpie': ['typo']}
        self.assertFalse(self.run_check(self.gateway())['ok'])
        self.assertTrue(self.run_check(self.gateway('https://gateway.example'))['optional_checks_skipped'])

    def test_settings_override_can_select_third_party(self):
        args = ['--settings', json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://gateway.example'}})]
        self.assertTrue(self.run_check(argv=args)['optional_checks_skipped'])

    def test_settings_override_cannot_hide_official_routing(self):
        args = ['--settings', json.dumps({'env': {'ANTHROPIC_BASE_URL': '', 'ANTHROPIC_AUTH_TOKEN': ''}})]
        r = self.run_check(self.gateway(), args)
        self.assertEqual(r['profile'], 'claudeai')
        self.assertFalse(r['ok'])

    def test_selected_sources_change_routing(self):
        self.settings.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://gateway.example'}}))
        self.assertTrue(self.run_check()['optional_checks_skipped'])
        self.assertEqual(self.run_check(argv=['--setting-sources', 'project'])['profile'], 'claudeai')

    def test_project_gateway_configuration_can_skip(self):
        project = self.cwd / '.claude'
        project.mkdir()
        (project / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://gateway.example'}}))
        self.assertTrue(self.run_check()['optional_checks_skipped'])

    def test_display_reports_authentication_and_optional_skip(self):
        stream = io.StringIO()
        p.display(self.run_check(self.gateway()), stream)
        self.assertIn('登录状态', stream.getvalue())
        self.assertIn('服务可选检查已跳过', stream.getvalue())

    def test_non_strict_policy_does_not_require_official_network_policy(self):
        path = self.root / 'policy.json'
        raw = {k: v for k, v in self.policy.items() if k not in ('proxy', 'expected_ips', 'allowed_countries')}
        path.write_text(json.dumps(raw))
        loaded = p.load_policy(path, strict=False)
        r = p.run_checks(loaded, [], self.gateway('https://gateway.example'), self.cwd)
        self.assertTrue(r['optional_checks_skipped'])
        r = p.run_checks(loaded, [], self.env, self.cwd)
        self.assertFalse(r['ok'])
        self.assertEqual(r['checks'][-1]['name'], 'policy')


if __name__ == '__main__':
    unittest.main()
