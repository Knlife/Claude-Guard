import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

source = Path(os.environ.get('CLAUDE_PREFLIGHT_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'preflight.py')))
spec = importlib.util.spec_from_file_location('preflight_directory_test', source)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class ConfigDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = dict(schema=1, real_binary='/bin/true', curl_binary='/usr/bin/curl',
                           proxy='http://127.0.0.1:11020', expected_ips=['203.0.113.1'],
                           allowed_countries=['US'])

    def test_config_dir_not_required_in_policy(self):
        path = self.root / 'policy.json'
        path.write_text(json.dumps(self.policy))
        self.assertEqual(p.load_policy(path)['real_binary'], '/bin/true')

    def test_old_machine_mapping_does_not_choose_config_directory(self):
        self.policy['claude_config_dir'] = '/old/default'
        self.policy['claude_config_dir_by_hostname'] = {'fixture': '/old/machine'}
        path = self.root / 'policy.json'
        path.write_text(json.dumps(self.policy))
        with patch.object(p.socket, 'gethostname', return_value='fixture'):
            policy = p.load_policy(path)
        env = {'CLAUDE_CONFIG_DIR': str(self.root), 'HTTPS_PROXY': self.policy['proxy']}
        result, checks = p.local_checks(policy, [], env, self.root)
        self.assertTrue(all(item['ok'] for item in checks))
        self.assertEqual(result['CLAUDE_CONFIG_DIR'], str(self.root))

    def test_changing_directory_changes_selected_settings(self):
        for name, base in (('one', 'https://one.example'), ('two', 'https://two.example')):
            directory = self.root / name
            directory.mkdir()
            (directory / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': base}}))
            env, settings = p.session_configuration(self.policy, [], {'CLAUDE_CONFIG_DIR': str(directory)}, self.root)
            self.assertEqual(env['ANTHROPIC_BASE_URL'], base)

    def test_relative_directory_uses_launch_working_directory(self):
        self.assertEqual(p.config_directory({'CLAUDE_CONFIG_DIR': 'profile'}, self.root), self.root / 'profile')

    def test_unset_directory_uses_claude_default(self):
        self.assertEqual(p.config_directory({}, self.root), Path.home() / '.claude')

    def test_official_directory_can_be_any_existing_directory(self):
        env = {'CLAUDE_CONFIG_DIR': str(self.root), 'HTTPS_PROXY': self.policy['proxy']}
        result, checks = p.local_checks(self.policy, [], env, self.root)
        self.assertTrue(all(item['ok'] for item in checks))
        self.assertEqual(p.environment_details(result, self.policy)['config_dir'], str(self.root))


if __name__ == '__main__':
    unittest.main()
