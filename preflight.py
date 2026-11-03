#!/usr/bin/env python3
"""Claude Guard: inspect the environment before starting Claude Code.

Python 3.10+, standard library only. No model generation requests are sent.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
from urllib.error import URLError

VERSION = "1.6.1"
DEFAULT_POLICY = Path(os.environ.get("CLAUDE_GUARD_POLICY") or
                      str(Path.home() / ".config/claude-guard/policy.json"))
MANAGED_CONFIG_DIR = Path("/etc/claude-code")
PROXY_KEYS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY")
AUTH_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
             "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_PROFILE")
PROBES = (
    ("api-exit", "https://api.anthropic.com/cdn-cgi/trace"),
    ("login-exit", "https://claude.ai/cdn-cgi/trace"),
    ("api-response", "https://api.anthropic.com/v1/messages"),
)
THIRD_PARTY = "https://fuck-claude.vercel.app/api/check?format=json&lang=zh"


class CheckFailure(Exception):
    def __init__(self, name, message, remedy, code=78):
        super().__init__(message)
        self.item = {"name": name, "ok": False, "message": message, "remedy": remedy}
        self.code = code


def read_object(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise CheckFailure("settings", f"无法读取有效 JSON：{path}", "修复该文件后重试。")
    if not isinstance(value, dict):
        raise CheckFailure("settings", f"JSON 顶层必须为对象：{path}", "修复该文件后重试。")
    return value


def load_policy(path, strict=True):
    policy = read_object(path)
    try:
        assert policy["schema"] == 1
        host_binaries = policy.get("real_binary_by_hostname", {})
        assert isinstance(host_binaries, dict)
        assert all(isinstance(host, str) and host and isinstance(binary, str)
                   and Path(binary).is_absolute()
                   for host, binary in host_binaries.items())
        policy["real_binary"] = host_binaries.get(
            socket.gethostname(), policy["real_binary"])
        # Host mappings identify Magpie for its optional checks. Actual session
        # routing, rather than the hostname, determines the check profile.
        host_gateways = policy.get("magpie_url_by_hostname", {})
        assert isinstance(host_gateways, dict)
        for host, value in host_gateways.items():
            assert isinstance(host, str) and host and isinstance(value, str)
            gateway = urlsplit(value)
            assert gateway.scheme == "http" and gateway.hostname in ("127.0.0.1", "::1", "localhost")
            assert gateway.port and not gateway.username and not gateway.password
            assert gateway.path in ("", "/") and not gateway.query and not gateway.fragment
        # Do not accept a machine-independent override from the policy file.
        policy["magpie_url"] = host_gateways.get(socket.gethostname(), "").rstrip("/")
        for key in ("real_binary", "curl_binary"):
            assert isinstance(policy[key], str) and Path(policy[key]).is_absolute()
        if not strict:
            policy["_policy_path"] = str(path)
            return policy
        proxy = urlsplit(policy["proxy"])
        assert proxy.scheme == "http" and proxy.hostname in ("127.0.0.1", "::1", "localhost")
        assert proxy.port and not proxy.username and not proxy.password
        assert proxy.path in ("", "/") and not proxy.query and not proxy.fragment
        assert isinstance(policy["expected_ips"], list) and policy["expected_ips"]
        policy["expected_ips"] = [str(ipaddress.ip_address(x)) for x in policy["expected_ips"]]
        assert isinstance(policy["allowed_countries"], list) and policy["allowed_countries"]
        assert all(isinstance(x, str) and re.fullmatch(r"[A-Z]{2}", x)
                   for x in policy["allowed_countries"])
        policy.setdefault("timeout_seconds", 6)
        policy.setdefault("connect_timeout_seconds", 2)
        policy.setdefault("attempts", 2)
        assert 0 < policy["connect_timeout_seconds"] <= policy["timeout_seconds"] <= 15
        assert type(policy["attempts"]) is int and 1 <= policy["attempts"] <= 2
    except (KeyError, TypeError, ValueError, AssertionError):
        raise CheckFailure("policy", "前置检查策略缺失或无效。", f"参考 policy.example.json 修复 {path}。")
    return policy


def settings_layers(config_dir, cwd, include_projects=True, sources=None):
    """Auth uses user/managed settings; Agent sessions also inspect projects."""
    sources = {"user", "project", "local"} if sources is None else sources
    user_path = config_dir / "settings.json"
    paths = [user_path] if "user" in sources else []
    if include_projects:
        for directory in reversed((cwd, *cwd.parents)):
            project_path = directory / ".claude/settings.json"
            if "project" in sources and project_path.resolve() != user_path.resolve():
                paths.append(project_path)
            if "local" in sources:
                paths.append(directory / ".claude/settings.local.json")
    paths.append(MANAGED_CONFIG_DIR / "managed-settings.json")
    paths.extend(sorted((MANAGED_CONFIG_DIR / "managed-settings.d").glob("*.json")))
    seen = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen and path.exists():
            seen.add(resolved)
            yield path, read_object(path)


def config_directory(env, cwd=None):
    cwd = Path.cwd() if cwd is None else Path(cwd)
    return (cwd / Path(env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))).resolve()


def settings_arguments(argv):
    selected = []
    index = 0
    while index < len(argv):
        if argv[index] == "--":
            break
        name, separator, value = argv[index].partition("=")
        if name in ("--settings", "--setting-sources"):
            selected.append(argv[index])
            if not separator:
                index += 1
                if index >= len(argv):
                    raise CheckFailure("settings-override", "临时配置参数缺少值。", "补全参数后重试。")
                selected.append(argv[index])
        elif not separator and name in {
                "--system-prompt", "--append-system-prompt", "--system-prompt-file",
                "--append-system-prompt-file", "--model", "--fallback-model", "--tools",
                "--allowedTools", "--disallowedTools", "--allowed-tools", "--disallowed-tools",
                "--mcp-config", "--permission-mode", "--permission-prompt-tool", "--agent",
                "--agents", "--input-format", "--output-format", "--json-schema",
                "--session-id", "--effort", "--thinking", "--thinking-display", "--debug-file"}:
            # A flag-looking prompt/tool value is data, not a settings option.
            index += 1
        index += 1
    return selected


def claude_command(argv):
    command = list(argv)
    index = 0
    while index < len(command):
        if command[index] == "--":
            break
        name, separator, value = command[index].partition("=")
        if name in ("--settings", "--setting-sources"):
            del command[index:index + (1 if separator else 2)]
        else:
            index += 1
    return command


def is_authentication_command(argv):
    command = claude_command(argv)
    return (command[:2] in (["auth", "login"], ["auth", "logout"], ["auth", "status"])
            or command[:1] == ["setup-token"])


def authentication_check(policy, argv, env, cwd=None):
    """Ask the real CLI about auth; never expose its account details or stderr."""
    config_args = settings_arguments(argv)
    # Authentication management must remain accessible before signing in.
    command = claude_command(argv)
    maintenance = (is_authentication_command(argv)
                   or command in (["--version"], ["-v"], ["--help"], ["-h"]))
    if maintenance:
        return {"name": "authentication", "ok": True, "skipped": True,
                "message": "认证管理或帮助命令可在未登录时执行。"}
    binary = Path(policy["real_binary"])
    if (not binary.is_file() or not os.access(binary, os.X_OK)
            or binary.resolve().parent == Path(__file__).resolve().parent / "bin"):
        raise CheckFailure("executable", "真实 Claude 程序无效或指向了检查入口。",
                           "修复策略中的 real_binary。")
    auth_env = dict(env)
    auth_env["CLAUDE_CONFIG_DIR"] = str(config_directory(env, cwd))
    try:
        result = subprocess.run([str(binary), *config_args, "auth", "status", "--json"],
                                env=auth_env, cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        status = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise CheckFailure("authentication", "无法确认当前配置目录的认证状态。",
                           "在当前 CLAUDE_CONFIG_DIR 下运行真实 Claude 的 auth status 排查。", 69)
    if not isinstance(status, dict) or status.get("loggedIn") is not True or result.returncode != 0:
        raise CheckFailure("authentication", "当前配置目录尚未登录或未配置可用认证来源。",
                           "在当前 CLAUDE_CONFIG_DIR 下执行 claude auth login，或配置所用服务的 API 凭据。", 77)
    return {"name": "authentication", "ok": True,
            "message": "Claude 已确认当前配置目录存在认证来源；未验证远端凭据有效性。"}


def bypasses(host, value):
    for entry in re.split(r"[,\s]+", value.strip()):
        if not entry:
            continue
        entry = entry.lower()
        if entry == "*":
            return True
        # Match conservatively: clients differ in wildcard and port handling.
        if entry.count(":") == 1:
            entry = entry.split(":")[0]
        entry = entry.removeprefix("*.").lstrip(".")
        if entry == host or host.endswith("." + entry):
            return True
    return False


def session_configuration(policy, argv, inherited=None, cwd=None, protect_projects=False):
    """Resolve enabled files + CLI settings + managed policy for every check."""
    env = dict(os.environ if inherited is None else inherited)
    cwd = Path.cwd() if cwd is None else Path(cwd)
    config_dir = config_directory(env, cwd)
    sources = {"user", "project", "local"}
    override = None
    config_args = settings_arguments(argv)
    index = 0
    while index < len(config_args):
        name, separator, value = config_args[index].partition("=")
        if name in ("--settings", "--setting-sources"):
            if not separator:
                index += 1
                if index >= len(config_args):
                    raise CheckFailure("settings-override", "临时配置参数缺少值。", "补全参数后重试。")
                value = config_args[index]
            if name == "--setting-sources":
                sources = {part.strip() for part in value.split(",")} if value else set()
                if not sources <= {"user", "project", "local"}:
                    raise CheckFailure("settings-override", "未知的配置来源。", "使用 user,project,local。")
            else:
                override = value
        index += 1
    merged = {}
    layers = []
    managed = []
    for path, layer in settings_layers(config_dir, cwd, include_projects=not is_authentication_command(argv), sources=sources):
        if path.is_relative_to(MANAGED_CONFIG_DIR):
            managed.append(("managed", path, layer))
            continue
        source = ("user" if path.resolve() == (config_dir / "settings.json").resolve()
                  else "local" if path.name == "settings.local.json" else "project")
        if source in sources:
            layers.append((source, path, layer))
    if override is not None:
        try:
            layer = json.loads(override)
        except ValueError:
            if override.lstrip().startswith(("{", "[")):
                raise CheckFailure("settings-override", "--settings 的临时 JSON 无效。", "修复 JSON 格式后重试；不会显示配置内容。")
            try:
                layer = read_object(cwd / override)
            except CheckFailure:
                raise CheckFailure("settings-override", "无法读取有效的 --settings 配置文件。", "使用可读的 JSON 对象文件。")
        if not isinstance(layer, dict):
            raise CheckFailure("settings-override", "--settings 顶层必须为 JSON 对象。", "使用 JSON 对象或包含该对象的文件。")
        layers.append(("flag", None, layer))
    protected = {*PROXY_KEYS, *AUTH_KEYS, "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                 "NODE_TLS_REJECT_UNAUTHORIZED", "NO_PROXY", "no_proxy"}
    for source, path, layer in [*layers, *managed]:
        values = layer.get("env", {})
        if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
            raise CheckFailure("settings-env", "env 必须是字符串映射。", "修复 Claude 配置后重试。")
        if "CLAUDE_CONFIG_DIR" in values:
            raise CheckFailure("claude-home", "settings.env 中不应再次设置 CLAUDE_CONFIG_DIR。",
                               "在启动 Claude 的 shell 环境中选择配置目录。")
        if protect_projects and source in ("project", "local") and any(
                env.get(key, "") != value for key, value in values.items() if key in protected):
            raise CheckFailure("project-network", f"项目文件覆盖了账户/网络配置：{path}",
                               "将这些配置统一到用户 settings.json；项目保留权限、Hook 等设置。")
        if "forceLoginMethod" in layer and layer["forceLoginMethod"] not in (None, "claudeai", "console", "gateway"):
            raise CheckFailure("settings-env", "forceLoginMethod 配置无效。", "使用 claudeai、console 或 gateway。")
        if "apiKeyHelper" in layer and not isinstance(layer["apiKeyHelper"], str):
            raise CheckFailure("settings-env", "apiKeyHelper 必须为字符串。", "修复认证来源配置。")
        env.update(values)
        merged.update({k: v for k, v in layer.items() if k != "env"})
    return env, merged


def session_profile(policy, env, settings, argv):
    # Account login/token setup contacts the official account service directly.
    command = claude_command(argv)
    if command[:2] == ["auth", "login"] or command[:1] == ["setup-token"]:
        return "claudeai"
    if any(env.get(key, "").lower() in ("1", "true") for key in
           ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        return "third-party"
    base = env.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
    if base:
        try:
            url = urlsplit(base)
            host = (url.hostname or "").lower().rstrip(".")
        except ValueError:
            host = ""
        official = any(host == domain or host.endswith("." + domain)
                       for domain in ("anthropic.com", "claude.ai"))
        if not official:
            return "magpie" if base == policy.get("magpie_url") else "third-party"
    if (settings.get("forceLoginMethod") == "console" or settings.get("apiKeyHelper")
            or any(env.get(key, "").strip() for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
            or re.search(r"(?mi)^\s*(authorization|x-api-key)\s*:", env.get("ANTHROPIC_CUSTOM_HEADERS", ""))):
        return "api"
    return "claudeai"


def optional_checks(policy, profile, env):
    choices = policy.get("optional_checks", {})
    selected = choices.get(profile, []) if isinstance(choices, dict) else None
    supported = {"tls", "magpie-gateway"} if profile == "magpie" else {"tls"}
    if (not isinstance(selected, list) or any(not isinstance(name, str) or name not in supported
                                             for name in selected)):
        raise CheckFailure("policy", "可选检查配置无效。",
                           'optional_checks 使用数组；可选 tls，Magpie 还可选 magpie-gateway。')
    checks = []
    for name in dict.fromkeys(selected):
        if name == "tls":
            ok = env.get("NODE_TLS_REJECT_UNAUTHORIZED") != "0"
            checks.append({"name": "tls", "ok": ok,
                           "message": "未关闭 TLS 证书校验。" if ok else "TLS 证书校验已关闭。",
                           "remedy": "删除 NODE_TLS_REJECT_UNAUTHORIZED=0。"})
        else:
            checks.append(magpie_probe(policy, env))
    return checks


def local_checks(policy, argv, inherited=None, cwd=None):
    env = dict(os.environ if inherited is None else inherited)
    cwd = Path.cwd() if cwd is None else Path(cwd)
    command = claude_command(argv)
    login_operation = command[:2] == ["auth", "login"] or command[:1] == ["setup-token"]
    config_dir = config_directory(env, cwd)
    if not config_dir.is_dir():
        raise CheckFailure("claude-home", "当前 CLAUDE_CONFIG_DIR 对应的目录不存在。",
                           "检查当前配置目录是否正确，以及共享盘是否已挂载。")
    env, merged = session_configuration(policy, argv, inherited, cwd, protect_projects=True)
    # An existing claude.ai login does not require an explicit login restriction.
    if merged.get("forceLoginMethod") not in (None, "claudeai"):
        raise CheckFailure("account-mode", "forceLoginMethod 与当前 claude.ai 模式冲突。",
                           "移除冲突的登录限制，或配置实际使用的 API/第三方服务来源。")
    active_auth = [key for key in AUTH_KEYS if env.get(key, "").strip()]
    base = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if base and base != "https://api.anthropic.com":
        active_auth.append("ANTHROPIC_BASE_URL")
    headers = env.get("ANTHROPIC_CUSTOM_HEADERS", "").lower()
    if re.search(r"(?m)^\s*(authorization|x-api-key)\s*:", headers):
        active_auth.append("ANTHROPIC_CUSTOM_HEADERS")
    if merged.get("apiKeyHelper"):
        active_auth.append("apiKeyHelper")
    if active_auth and not login_operation:
        raise CheckFailure("account-source", "存在其他认证/服务来源：" + ", ".join(active_auth),
                           "清理对应环境变量或 settings；旧 tmux 中的环境也需清理。不会显示变量值。")
    if env.get("NODE_TLS_REJECT_UNAUTHORIZED") == "0":
        raise CheckFailure("tls", "Claude 的 TLS 证书校验被关闭。", "删除 NODE_TLS_REJECT_UNAUTHORIZED=0。")
    if not any(env.get(k) for k in PROXY_KEYS[:4]):
        raise CheckFailure("proxy-env", "Claude 的 HTTP(S) 代理环境未设置。", "恢复本机代理启动环境。")
    conflicts = [key for key in PROXY_KEYS if env.get(key) and env[key] != policy["proxy"]]
    if conflicts:
        raise CheckFailure("proxy-env", "代理变量与固定策略不一致：" + ", ".join(conflicts),
                           "恢复策略中的本地 HTTP 代理；不自动切换出口。")
    for key in ("NO_PROXY", "no_proxy"):
        if any(bypasses(host, env.get(key, "")) for host in ("claude.ai", "api.anthropic.com")):
            raise CheckFailure("proxy-bypass", f"{key} 会让 Claude 域名绕过代理。",
                               "保留内网条目，移除通配符或 Claude/Anthropic 域名。")
    for key in ("real_binary", "curl_binary"):
        path = Path(policy[key])
        if not path.is_file() or not os.access(path, os.X_OK):
            raise CheckFailure("executable", f"策略指定的 {key} 不存在或不能执行。",
                               "检查安装或更新 preflight.json 中的绝对路径。")
    # Never execute another copy of the wrapper recursively.
    if Path(policy["real_binary"]).resolve().parent == Path(__file__).resolve().parent / "bin":
        raise CheckFailure("recursive-wrapper", "real_binary 指向了检查入口。", "将它指向 Claude 原始可执行文件。")
    return env, [{"name": "local-config", "ok": True,
                  "message": "Claude 目录、账户来源、代理与 TLS 配置通过。"}]


def fetch(policy, url, env):
    """Explicit proxy, verified TLS, no curlrc, redirects, auth headers or cookies."""
    command = [policy["curl_binary"], "-q", "--silent", "--show-error",
               "--connect-timeout", str(policy["connect_timeout_seconds"]),
               "--max-time", str(policy["timeout_seconds"]),
               "--proto", "=https", "--proxy", policy["proxy"], "--noproxy", "",
               "--max-filesize", "65536", "--header", "Cache-Control: no-cache",
               "--write-out", "\n%{http_code} %{time_total}", url]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, env=env, timeout=policy["timeout_seconds"] + 1)
    except subprocess.TimeoutExpired:
        return {"transport": 28, "status": 0, "body": "", "seconds": policy["timeout_seconds"]}
    except OSError:
        return {"transport": 127, "status": 0, "body": "", "seconds": 0}
    try:
        body, trailer = result.stdout.rsplit("\n", 1)
        status, seconds = trailer.split()
        return {"transport": result.returncode, "status": int(status), "body": body,
                "seconds": float(seconds)}
    except ValueError:
        return {"transport": result.returncode or 65, "status": 0, "body": "", "seconds": 0}


def probe(policy, name, url, env):
    for attempt in range(policy["attempts"]):
        response = fetch(policy, url, env)
        retryable = response["transport"] in (7, 28, 35, 52, 56) or response["status"] in (502, 503, 504)
        if not retryable or attempt + 1 == policy["attempts"]:
            break
    item = {"name": name, "ok": False, "milliseconds": round(response["seconds"] * 1000),
            "attempts": attempt + 1, "http_status": response["status"]}
    if response["transport"]:
        item.update(message=f"连接/TLS 检查失败（curl {response['transport']}）。",
                    remedy="检查本地代理、端口转发或上游网络；不会回退直连。")
        return item
    if name.endswith("-exit"):
        trace = dict(line.split("=", 1) for line in response["body"].splitlines() if "=" in line)
        host = urlsplit(url).hostname
        try:
            exit_ip = str(ipaddress.ip_address(trace["ip"]))
        except (KeyError, ValueError):
            exit_ip = None
        valid = (response["status"] == 200 and trace.get("h") == host
                 and trace.get("visit_scheme") == "https" and trace.get("tls", "").startswith("TLSv"))
        if not valid or not exit_ip or not trace.get("loc"):
            item.update(message="官方域名的出口响应缺失或格式不符；不能确认出口。",
                        remedy="检查官方 trace 接口或代理状态；缺失信息不会视为通过。")
        elif exit_ip not in policy["expected_ips"] or trace["loc"] not in policy["allowed_countries"]:
            item.update(message=f"出口不在允许清单：{exit_ip} / {trace['loc']}。",
                        remedy="恢复纯净 IP 分组；只有主动更换 ISP 后才更新允许清单。")
        else:
            item.update(ok=True, message=f"{host} → {exit_ip} / {trace['loc']}",
                        exit_ip=exit_ip, country=trace["loc"])
    else:
        try:
            body = json.loads(response["body"])
            error_type = body.get("error", {}).get("type") if isinstance(body, dict) else None
        except (ValueError, AttributeError):
            error_type = None
        expected = ((response["status"] == 405 and error_type == "invalid_request_error")
                    or (response["status"] == 401 and error_type == "authentication_error"))
        if expected:
            item.update(ok=True, message=f"官方 API 可达（无凭据 GET 返回预期 HTTP {response['status']}）。")
        else:
            item.update(message=f"官方 API 响应不符合预期（HTTP {response['status']}）。",
                        remedy="检查官方服务状态或代理；403、429、5xx 不视为通过。")
    return item


def magpie_probe(policy, env):
    started = time.monotonic()
    item = {"name": "magpie-gateway", "ok": False}
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        request = Request(policy["magpie_url"] + "/v1/models",
                          headers={"Authorization": "Bearer magpie", "Accept": "application/json"})
        # The explicitly approved loopback endpoint must not enter the WAN proxy.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=6) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError("unexpected gateway response")
            catalog = json.loads(body)
            ids = {entry["id"] for entry in catalog["data"] if isinstance(entry, dict)
                   and isinstance(entry.get("id"), str)}
            if not ids:
                raise ValueError("empty model catalog")
            item.update(ok=True, http_status=200,
                        message=f"Magpie 隧道可达，模型目录 {len(ids)} 项；未发送生成请求。")
    except (OSError, URLError, ValueError, KeyError, TypeError):
        item.update(message="Magpie 隧道不可达或返回无效模型目录。",
                    remedy="检查策略指定的 Magpie 服务或端口转发；不回退官方接口。")
    item["milliseconds"] = round((time.monotonic() - started) * 1000)
    return item


def run_checks(policy, argv, inherited=None, cwd=None):
    started = time.monotonic()
    report = {"version": VERSION, "ok": False, "checks": []}
    try:
        env, settings = session_configuration(policy, argv, inherited, cwd)
        report["config_dir"] = str(config_directory(env, cwd))
        profile = session_profile(policy, env, settings, argv)
        report["profile"] = profile
        if profile != "claudeai":
            report["checks"].append(authentication_check(policy, argv, env, cwd))
            selected = optional_checks(policy, profile, env)
            report["checks"].extend(selected)
            report["optional_checks_skipped"] = not selected
            report["ok"] = all(item["ok"] for item in report["checks"])
            report["code"] = 0 if report["ok"] else 69
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return report
        if policy.get("_policy_path"):
            policy = load_policy(Path(policy["_policy_path"]))
        env, report["checks"] = local_checks(policy, argv, inherited, cwd)
        report["environment"] = environment_details(env, policy)
        address = urlsplit(policy["proxy"])
        try:
            with socket.create_connection((address.hostname, address.port), timeout=0.5):
                pass
        except OSError:
            raise CheckFailure("proxy-port", "本地代理端口未连通。",
                               "检查策略指定的本地代理服务或 SSH 端口转发。", 69)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
            futures = [pool.submit(probe, policy, name, url, env) for name, url in PROBES]
            report["checks"].extend(f.result() for f in futures)
        if all(item["ok"] for item in report["checks"]):
            report["checks"].append(authentication_check(policy, argv, env, cwd))
        report["ok"] = all(x["ok"] for x in report["checks"])
        report["code"] = 0 if report["ok"] else 69
    except CheckFailure as exc:
        report["checks"].append(exc.item)
        report["code"] = exc.code
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def system_timezone():
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        return "unknown"


def environment_details(env, policy):
    source = "LC_ALL" if env.get("LC_ALL") else "LANG" if env.get("LANG") else ""
    return {
        "timezone": env.get("TZ") or system_timezone(),
        "locale": env.get("LC_ALL") or env.get("LANG") or "unknown",
        "locale_source": source,
        "lc_all": env.get("LC_ALL", ""),
        "lang": env.get("LANG", ""),
        "language": env.get("LANGUAGE", ""),
        "config_dir": str(config_directory(env)),
        "proxy": policy["proxy"],
    }


CHECK_LABELS = {
    "authentication": "登录状态",
    "magpie-gateway": "Magpie 隧道",
    "local-config": "本地配置", "api-exit": "API 出口", "login-exit": "登录出口",
    "api-response": "API 连通", "proxy-port": "代理端口", "settings": "配置文件",
    "policy": "启动策略", "settings-override": "临时配置", "claude-home": "配置目录",
    "settings-env": "配置变量", "account-mode": "登录方式", "account-source": "认证来源",
    "tls": "证书校验", "proxy-env": "代理配置", "proxy-bypass": "代理绕过",
    "executable": "执行文件", "recursive-wrapper": "启动入口", "project-network": "项目配置",
}


def terminal_text(value):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    return "".join(char if char == "\n" or unicodedata.category(char) != "Cc" else " "
                   for char in text)


def text_width(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def wrap_terminal(value, width):
    lines = []
    for paragraph in terminal_text(value).split("\n"):
        line, used = "", 0
        # Keep addresses, paths and English words together when they fit a row.
        tokens = re.findall(r"[A-Za-z0-9_./:@%+=?&#~\[\]-]+|[^\S\n]+|.", paragraph)
        for token in tokens:
            if line and used + text_width(token) > width:
                lines.append(line.rstrip())
                line, used = "", 0
            if token.isspace() and not line:
                continue
            for char in token:
                size = text_width(char)
                if line and used + size > width:
                    lines.append(line.rstrip())
                    line, used = "", 0
                line += char
                used += size
        lines.append(line.rstrip())
    return lines or [""]


def display(report, stream=None):
    stream = sys.stdout if stream is None else stream
    columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    is_terminal = bool(getattr(stream, "isatty", lambda: False)())
    if is_terminal:
        try:
            columns = os.get_terminal_size(stream.fileno()).columns
        except (AttributeError, OSError):
            pass
    columns = max(40, columns)
    color = is_terminal and "NO_COLOR" not in os.environ
    color = color and os.environ.get("TERM") != "dumb"

    def paint(text, code):
        return f"\x1b[{code}m{text}\x1b[0m" if color else text

    def emit_wrapped(text, indent="", style=None):
        for line in wrap_terminal(text, columns - text_width(indent)):
            print(indent + (paint(line, style) if style else line), file=stream)

    rows = []
    environment = report.get("environment", {})
    for item in report["checks"]:
        detail = item["message"]
        if item["ok"] and item["name"] == "local-config":
            if environment.get("config_dir") and environment.get("proxy"):
                local_rows = [
                    ("配置目录", environment["config_dir"]),
                    ("登录方式", "claude.ai（账户模式）"),
                    ("认证来源", "未检测到其他认证覆盖"),
                    ("代理地址", environment["proxy"]),
                    ("证书校验", "未禁用 TLS 证书校验"),
                ]
                rows.extend((["PASS", label, value, "—"], "32") for label, value in local_rows)
            else:
                rows.append((["PASS", "本地配置", "目录、认证、代理、TLS 正常", "—"], "32"))
            for label, key in [("CLI语言", "locale"), ("LC_ALL", "lc_all"),
                               ("LANG", "lang"), ("LANGUAGE", "language"), ("系统时区", "timezone")]:
                if key in environment:
                    value = environment[key] or "未设置"
                    if key == "locale" and environment.get("locale_source"):
                        value += f" ({environment['locale_source']})"
                    rows.append((["INFO", label, value, "—"], "36"))
            continue
        elif item["ok"] and "exit_ip" in item:
            detail = f"{item['exit_ip']} / {item['country']}"
        elif item["ok"] and item["name"] == "api-response" and item.get("http_status"):
            detail = f"HTTP {item['http_status']} · 预期响应"
        timing = f"{item['milliseconds']} ms" if "milliseconds" in item else "—"
        if item.get("attempts", 1) > 1:
            timing += f"\n重试 {item['attempts'] - 1}"
        rows.append((["SKIP" if item.get("skipped") else "PASS" if item["ok"] else "FAIL",
                      CHECK_LABELS.get(item["name"], item["name"]), detail, timing],
                     "32" if item["ok"] else "31"))
    diagnostic = report.get("third_party")
    if diagnostic is not None:
        if diagnostic["ok"]:
            band = {"low": "低风险", "medium": "中等风险", "high": "高风险"}.get(diagnostic["band"], diagnostic["band"])
            detail = f"{diagnostic['score']}/100 · {band}（参考）"
        else:
            detail = "服务暂不可用，不影响核心检查"
        rows.append((["参考" if diagnostic["ok"] else "跳过", "第三方", detail, "—"], "33"))

    headers = ["状态", "检查项目", "结果", "耗时"]
    cells = [headers, *(values for values, _ in rows)]
    # Fit actual content instead of filling the terminal. Long paths wrap at 40
    # display columns, keeping both wide SSH terminals and narrow panes readable.
    caps, minimums = [6, 16, 40, 10], [4, 8, 8, 4]
    widths = [min(caps[column], max(text_width(line) for values in cells
                                   for line in terminal_text(values[column]).split("\n")))
              for column in range(4)]
    overflow = sum(widths) + 13 - columns
    for column in (2, 1, 3, 0):
        reduction = min(max(0, overflow), max(0, widths[column] - minimums[column]))
        widths[column] -= reduction
        overflow -= reduction
    columns = sum(widths) + 13
    ascii_box = os.environ.get("TERM") == "dumb"
    vertical, horizontal = ("|", "-") if ascii_box else ("│", "─")

    def border(left, middle, right):
        if ascii_box:
            left = middle = right = "+"
        line = left + middle.join(horizontal * (width + 2) for width in widths) + right
        print(paint(line, "2"), file=stream)

    def row(cells, status_color=None, header=False):
        wrapped = [wrap_terminal(cell, width) for cell, width in zip(cells, widths)]
        for index in range(max(map(len, wrapped))):
            parts = []
            for column, (lines, width) in enumerate(zip(wrapped, widths)):
                value = lines[index] if index < len(lines) else ""
                padding = " " * (width - text_width(value))
                value = padding + value if column == 3 else value + padding
                if header:
                    value = paint(value, "1")
                elif column == 0 and status_color:
                    value = paint(value, status_color)
                parts.append(" " + value + " ")
            print(vertical + vertical.join(parts) + vertical, file=stream)

    emit_wrapped(f"Claude Guard · 启动前检查  v{report.get('version', VERSION)}", style="1;36")
    border("┌", "┬", "┐")
    row(headers, header=True)
    border("├", "┼", "┤")
    for cells, status_color in rows:
        row(cells, status_color)
    border("└", "┴", "┘")
    passed = sum(bool(item["ok"]) for item in report["checks"] if not item.get("skipped"))
    total = sum(not item.get("skipped", False) for item in report["checks"])
    result = "检查通过" if report["ok"] else "检查未通过，已阻止启动 Claude"
    emit_wrapped(f"{result} · 核心检查 {passed}/{total} · 总耗时 {report.get('elapsed_seconds', 0):.2f}s",
                 style="1;32" if report["ok"] else "1;31")
    for item in report["checks"]:
        if not item["ok"]:
            emit_wrapped(f"处理建议 · {CHECK_LABELS.get(item['name'], item['name'])}：{item['remedy']}", indent="  ")
    if diagnostic is not None:
        emit_wrapped(diagnostic["note"], style="2")
    if report.get("optional_checks_skipped"):
        emit_wrapped("服务可选检查已跳过；登录状态仍按统一要求检查。", style="36")


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "launch"))
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="输出机器可读报告")
    parser.add_argument("--third-party", action="store_true", help="额外查询第三方评分，仅供参考")
    args_in = list(sys.argv[1:] if arguments is None else arguments)
    boundary = args_in.index("--") if "--" in args_in else len(args_in)
    args = parser.parse_args(args_in[:boundary])
    claude_args = args_in[boundary + 1:]
    try:
        policy = load_policy(args.policy, strict=False)
        report = run_checks(policy, claude_args)
        if args.third_party and report["ok"] and report.get("profile") == "claudeai":
            policy = load_policy(args.policy)
            env, _ = local_checks(policy, claude_args)
            response = fetch(policy, THIRD_PARTY, env)
            diagnostic = {"ok": False, "note": "参考结果，不决定是否放行；不是 Anthropic 官方风控。"}
            try:
                body = json.loads(response["body"])
                if response["transport"] == 0 and response["status"] == 200 and body.get("estimate") is True:
                    diagnostic.update(ok=True, score=body["score"], band=body["band"], coverage=body["coverage"])
            except (ValueError, KeyError, AttributeError):
                pass
            report["third_party"] = diagnostic
    except CheckFailure as exc:
        report = {"ok": False, "code": exc.code, "checks": [exc.item], "elapsed_seconds": 0}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        display(report, sys.stderr if args.mode == "launch" else sys.stdout)
    if not report["ok"]:
        return report["code"]
    if args.mode == "launch":
        # Preserve the user's cwd, stdin, TTY, argv, inherited env and exit status.
        os.execv(policy["real_binary"], [policy["real_binary"], *claude_args])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
