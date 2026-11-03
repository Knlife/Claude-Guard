# Claude Guard 工作指南

本仓库实现 Claude Code 启动前的环境检查。修改时以 `preflight.py`、`install.py` 和回归测试为准；用户使用说明见 [README.md](README.md)。

## 安装流程

适用环境为 Linux / POSIX Shell，需 Python 3.10+、真实 Claude Code 程序；官方网络检查还需 curl 和本地 HTTP 代理。

### 1. 确认真正的 Claude 程序

```sh
python3 --version
curl --version
type -a claude
```

检查 `claude` 是否已被别名、函数或旧包装器接管。安装器的 `--real-binary` 必须指向原始 Claude 程序；如果入口是软链接，可以使用它解析后的真实版本文件。不能把本仓库的 `bin/claude` 或已有检查包装器作为真实程序。

先用选定的绝对路径执行 `--version`，确认它可运行。后续命令中的 `/absolute/path/to/real/claude` 均为需要替换的占位路径。

### 2. 获取并安装

```sh
git clone https://github.com/Knlife/Claude-Guard.git claude-guard
cd claude-guard
python3 install.py --real-binary /absolute/path/to/real/claude
```

在已有 checkout 内工作时直接执行安装命令，无需再次 clone。

默认安装布局：

```text
~/.local/share/claude-guard/
├── preflight.py
└── bin/
    ├── claude
    ├── claude-guard
    └── check-claude

~/.config/claude-guard/policy.json
```

安装器首次生成策略，填入提供的真实程序路径和探测到的 curl 路径。已有策略完整保留，包括其中的 `real_binary`。安装器不修改 Shell 启动文件、Claude 登录状态或用户 settings。

### 3. 配置检查策略

编辑 `~/.config/claude-guard/policy.json`，结构见 [policy.example.json](policy.example.json)。

- 官方会话：配置实际 `proxy`、明确允许的 `expected_ips` 和 `allowed_countries`。示例 IP `203.0.113.10` 不能作为实际出口使用。
- `proxy` 是策略要求，不会自动设置进程代理。通过启动环境或启用的 settings `env` 设置实际代理，保持已设置的代理变量与策略一致。
- 第三方会话：由当前配置的 `ANTHROPIC_BASE_URL`、认证来源等决定类别；按 `optional_checks` 选择检查。空列表只跳过可选项，认证状态仍需通过。
- `CLAUDE_CONFIG_DIR` 由调用环境选择，不设置按机器固定的账户目录，也不把它写入 settings 的 `env`。
- 不为使检查通过而自动扩大出口允许清单或关闭 TLS 校验。

需要自定义策略路径时，安装和运行都显式指定：

```sh
python3 install.py \
  --real-binary /absolute/path/to/real/claude \
  --policy /absolute/path/to/policy.json
export CLAUDE_GUARD_POLICY=/absolute/path/to/policy.json
```

### 4. 启用与验证

```sh
export PATH="$HOME/.local/share/claude-guard/bin:$PATH"
type -a claude
claude-guard --json
claude --version
```

确认实际 `claude` 入口来自新安装目录；同名 Shell 函数或别名优先于 PATH，需要调整其指向。需要持久化时，将 PATH 设置加入对应 Shell 配置。保留用户显式选择的 API、代理和认证环境变量。

检查命令不会启动对话。官方会话必须通过网络与认证检查；第三方会话应显示正确的服务类别，认证通过后只运行已选择的可选项。不要把 `hasCompletedOnboarding: true` 当成登录或 API 配置。

最后由用户按实际用途启动 `claude`，或让 SDK / Magpie 启动 worker。未登录时可以执行 `claude auth login`；不得代替用户提供或伪造官方授权信息。

### 5. 更新和迁移

更新 checkout 后重新运行安装命令；已有策略不会被覆盖。Claude 升级导致真实二进制路径改变时，要同步核对并更新策略中的 `real_binary`，仅重跑安装器不会改写已有值。

从旧安装迁移时，可以在本机把已有策略保存到新的策略路径，或通过 `CLAUDE_GUARD_POLICY` 指定旧策略，再调整入口。不要把实际策略、账户目录、日志或备份加入仓库。安装问题先检查 PATH、同名函数、当前策略路径、当前 `CLAUDE_CONFIG_DIR` 和 `claude-guard --json` 的失败项。

## 修改代码的边界

- 官方会话保留代理、TLS、出口、认证来源和登录状态检查。
- 配置解析同时用于服务判定和正式检查，避免两套来源产生不同结果。
- 认证命令使用继承环境、选定用户配置和系统管理配置，不扫描无关父项目。
- `--setting-sources ''` 确实禁用 user/project/local 文件；禁用文件不能被读取或解析。系统管理策略始终生效。
- 临时 settings 需要校验 JSON、环境变量类型和安全相关字段，不能按参数名一概拒绝，也不能无条件放行。
- 保留原始 CLI 参数、空字符串、工作目录、stdin/stdout。报告写入 stderr，SDK 的 stdout 只能交给真实 Claude。
- 认证探测使用真实程序的 `auth status --json`，不能递归调用包装入口，也不能消耗 worker 的 stdin。

## 测试

默认验证不需要账号、网络或模型调用：

```sh
python3 -B -m unittest discover -s tests -v
git diff --check
```

修改安装器或入口时，保留安装目录含空格、重复安装保留策略、禁止覆盖真实程序、参数和 stdin 完整转发的测试。

修改 SDK 兼容性时，还需在可用的官方策略和已认证目录上显式验证 initialize：

```sh
python3 tests/verify_sdk_initialize.py \
  --policy /absolute/path/to/policy.json \
  --binary /absolute/path/to/real/claude \
  --config-dir /absolute/path/to/authenticated/profile
```

此脚本会联网执行官方检查，只发送 `control_request.initialize`，收到成功响应后结束；不发送用户消息。报告实际验证的 Claude 版本和协议阶段，不能以 `--help` 成功代替 SDK initialize 成功。该验证不在默认 CI 中执行。
