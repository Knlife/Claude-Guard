import hashlib
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

default_source = (Path(__file__).resolve().parents[1] / 'preflight.py'
                  if Path(__file__).parent.name == 'tests' else Path(__file__).with_name('preflight.py'))
parser = argparse.ArgumentParser(description='Verify Magpie SDK initialize without sending user/model messages; requires an authenticated config directory.')
parser.add_argument('--source', type=Path, default=Path(os.environ.get('CLAUDE_PREFLIGHT_TEST_SOURCE', str(default_source))))
parser.add_argument('--policy', type=Path, required=True)
parser.add_argument('--binary', required=True, help='Absolute path to the real Claude Code executable')
parser.add_argument('--config-dir', required=True, help='An existing authenticated Claude configuration directory')
parser.add_argument('--model', default='sonnet')
args = parser.parse_args()
source = args.source
policy = json.loads(args.policy.read_text())
policy['real_binary'] = args.binary
policy.pop('real_binary_by_hostname', None)
config = args.config_dir
user_settings = Path(config) / 'settings.json'
before = hashlib.sha256(user_settings.read_bytes()).hexdigest() if user_settings.exists() else None
with tempfile.TemporaryDirectory(prefix='preflight-sdk-initialize-') as directory:
    root = Path(directory)
    policy_path = root / 'policy.json'
    policy_path.write_text(json.dumps(policy))
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config)
    for key in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                'CLAUDE_CODE_USE_FOUNDRY', 'ANTHROPIC_PROFILE', 'ANTHROPIC_CUSTOM_HEADERS',
                'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SSE_PORT',
                'NODE_TLS_REJECT_UNAUTHORIZED', 'NO_PROXY', 'no_proxy'):
        env.pop(key, None)
    for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
        env[key] = policy['proxy']
    env.update(IS_SANDBOX='1', ENABLE_CLAUDEAI_MCP_SERVERS='0', DISABLE_AUTO_COMPACT='1',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_AUTO_MEMORY='1',
               CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS='1', CLAUDE_CODE_PROMPT_CACHE_TTL='1h',
               DISABLE_AUTOUPDATER='1', NO_PROXY='localhost,127.0.0.1', no_proxy='localhost,127.0.0.1')
    # Exact claudeCLIArgs structure and settings constant from Magpie commit
    # c86fa2a341628ff3e12da15df2b9c4ed60821000 (0.1.2766).
    argv = ['-p', '--output-format', 'stream-json', '--input-format', 'stream-json',
            '--include-partial-messages', '--verbose', '--model', args.model,
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--settings', '{"disableDeepLinkRegistration":"disable"}',
            '--dangerously-skip-permissions', '--no-session-persistence']
    stderr = root / 'stderr.log'
    with stderr.open('w') as log:
        child = subprocess.Popen([sys.executable, str(source), 'launch', '--policy', str(policy_path), '--', *argv],
                                 cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=log, start_new_session=True)
        message = {'type': 'control_request', 'request_id': 'preflight-sdk-initialize',
                   'request': {'subtype': 'initialize'}}
        # Queue input before preflight finishes; its probes must not consume it.
        child.stdin.write((json.dumps(message) + '\n').encode())
        child.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        response = None
        pending = b''
        deadline = time.monotonic() + 40
        count = 0
        try:
            while time.monotonic() < deadline and response is None:
                for key, event in selector.select(timeout=0.2):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    pending += data
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        count += 1
                        if item.get('type') == 'control_response' and item.get('response', {}).get('request_id') == message['request_id']:
                            response = item['response']
                if child.poll() is not None:
                    break
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=8)
            selector.close()
            child.stdin.close()
            child.stdout.close()
    if response is None or response.get('subtype') != 'success':
        # Only diagnostics are printed; successful initialize carries account
        # details which intentionally never leave this local test process.
        print('initialize failed; exit:', child.returncode)
        print(stderr.read_text()[-4500:])
        raise SystemExit(1)
    assert '核心检查 5/5' in stderr.read_text(), 'Official strict checks were not all verified'
    print('Claude Code SDK initialize: success')
    print('official preflight checks: 5/5; stdout JSON records:', count)
    print('only control_request.initialize sent; no user/model request sent')
after = hashlib.sha256(user_settings.read_bytes()).hexdigest() if user_settings.exists() else None
assert before == after, 'Selected user settings changed during verification'
print('Selected user settings unchanged:', after)
