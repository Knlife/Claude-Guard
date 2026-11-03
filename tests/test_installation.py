import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / 'install with spaces'
        self.policy = self.root / 'config/policy.json'
        self.binary = self.root / 'native-claude'
        self.binary.write_text('#!' + sys.executable + '\n'
                               'import json,sys\n'
                               'if sys.argv[-3:]==["auth","status","--json"]:\n'
                               ' print(json.dumps({"loggedIn":True}))\n'
                               'else:\n'
                               ' print(json.dumps({"argv":sys.argv[1:],"input":sys.stdin.read()}))\n')
        self.binary.chmod(0o755)

    def install(self, binary=None):
        return subprocess.run([sys.executable, str(ROOT / 'install.py'), '--real-binary', str(binary or self.binary),
                               '--prefix', str(self.prefix), '--policy', str(self.policy)],
                              text=True, capture_output=True, timeout=10)

    def test_installed_wrapper_preserves_arguments_and_stdin(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / 'profile'
        config.mkdir()
        env = dict(os.environ, CLAUDE_GUARD_POLICY=str(self.policy), CLAUDE_CONFIG_DIR=str(config),
                   ANTHROPIC_BASE_URL='https://gateway.example', ANTHROPIC_AUTH_TOKEN='test-placeholder')
        args = ['--setting-sources', '', '--settings', '{}', '-p', '--model', 'model with spaces']
        run = subprocess.run([str(self.prefix / 'bin/claude'), *args], input='control-input\n',
                             env=env, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        body = json.loads(run.stdout)
        self.assertEqual(body['argv'], args)
        self.assertEqual(body['input'], 'control-input\n')
        self.assertIn('Claude Guard', run.stderr)
        check = subprocess.run([str(self.prefix / 'bin/check-claude'), '--json'],
                               env=env, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(json.loads(check.stdout)['ok'])

    def test_reinstall_preserves_existing_policy_bytes(self):
        self.assertEqual(self.install().returncode, 0)
        content = self.policy.read_text().replace('203.0.113.10', '203.0.113.20')
        self.policy.write_text(content)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.policy.read_text(), content)

    def test_installer_does_not_overwrite_the_real_binary(self):
        target = self.prefix / 'bin/claude'
        target.parent.mkdir(parents=True)
        target.write_bytes(self.binary.read_bytes())
        target.chmod(0o755)
        before = target.read_bytes()
        result = self.install(target)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
