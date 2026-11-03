#!/usr/bin/env python3
"""Install Claude Guard without changing shell profiles or Claude accounts."""
import argparse
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-binary', type=Path, required=True,
                        help='Absolute path to the real Claude Code binary, not a precheck wrapper')
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/claude-guard')
    parser.add_argument('--policy', type=Path, default=Path.home() / '.config/claude-guard/policy.json')
    args = parser.parse_args()
    binary = args.real_binary.expanduser().resolve()
    prefix = args.prefix.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('--real-binary must name an executable Claude Code binary')
    if binary == prefix / 'bin/claude' or binary.parent == ROOT / 'bin':
        parser.error('--real-binary points to a wrapper; select the original Claude binary')
    if prefix == ROOT:
        parser.error('--prefix must differ from the source checkout')
    (prefix / 'bin').mkdir(parents=True, exist_ok=True)
    for relative in ('preflight.py', 'bin/claude', 'bin/claude-guard', 'bin/check-claude'):
        target = prefix / relative
        temporary = target.with_name(target.name + '.new')
        shutil.copy2(ROOT / relative, temporary)
        temporary.chmod(0o755)
        temporary.replace(target)
    if not policy_path.exists():
        policy = json.loads((ROOT / 'policy.example.json').read_text())
        policy['real_binary'] = str(binary)
        policy['curl_binary'] = shutil.which('curl') or '/usr/bin/curl'
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        with policy_path.open('x') as stream:
            json.dump(policy, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        policy_path.chmod(0o600)
        print('Created example policy. Set proxy, expected_ips and allowed_countries before checking official sessions.')
    else:
        print('Preserved existing policy, including its real_binary setting.')
    print('Installed:', prefix)
    print('Policy:', policy_path)
    print('Prepend to PATH:', prefix / 'bin')
    print('For a custom policy path, set CLAUDE_GUARD_POLICY to that path.')


if __name__ == '__main__':
    main()
