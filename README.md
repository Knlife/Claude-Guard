# Claude Guard

Claude Code 启动前的环境检查工具。官方 `claude.ai` 会话执行严格网络与认证检查；API、Magpie 和其他第三方服务按配置执行可选检查，同时确认 Claude 自身报告的认证状态。

支持交互式 CLI、认证命令和非交互 SDK worker，包括 `--setting-sources ''`、`--settings`、stream-json 输入输出。实现只依赖 Python 标准库，主要运行环境为 Linux / POSIX Shell。

## 环境要求

- Python 3.10 或更新版本。
- 已安装的真实 Claude Code 可执行文件。
- 官方网络检查需要 curl 和可用的本地 HTTP 代理。

SDK 初始化兼容性曾使用 Claude Code 2.1.292、Magpie 0.1.2766 的启动参数验证；普通入口也验证过 Claude Code 2.1.316。兼容性验证只发送 `initialize` 控制请求，不发送用户消息。

## 安装

```sh
git clone https://github.com/Knlife/claude-guard.git
cd claude-guard
python3 install.py --real-binary /absolute/path/to/real/claude
```

将 `/absolute/path/to/real/claude` 替换成真实 Claude 程序的绝对路径。安装器把程序写入 `~/.local/share/claude-guard`，首次生成 `~/.config/claude-guard/policy.json`，已有策略会保留。

编辑生成的策略，设置自己的 `proxy`、`expected_ips` 和 `allowed_countries`。示例中的 `203.0.113.10` 是文档地址，必须替换成你明确允许的实际出口 IP。

供 agent 执行的完整安装、验证、更新和迁移步骤见 [AGENTS.md](AGENTS.md)。

然后将新入口加入 PATH：

```sh
export PATH="$HOME/.local/share/claude-guard/bin:$PATH"
type -a claude
claude-guard --json
claude
```

`type -a claude` 用于确认实际入口。如果 Shell 已定义同名函数或别名，需要自行调整其指向。安装器不会编辑 Shell 启动文件、登录凭据或 Claude 用户配置。

| 入口 | 行为 |
| --- | --- |
| `claude-guard` | 检查并输出报告，不启动 Claude |
| `check-claude` | `claude-guard` 的兼容入口 |
| `claude` | 检查成功后，用原参数、工作目录、终端和输入输出流执行真实 Claude |

可以直接从源码目录运行 `bin/claude-guard` 和 `bin/claude`。自定义策略通过以下变量指定：

```sh
export CLAUDE_GUARD_POLICY=/absolute/path/to/policy.json
```

检查命令也接受 `--policy /absolute/path/to/policy.json`。自定义安装位置使用 `install.py --prefix /absolute/path/to/install`，再把该目录的 `bin` 加入 PATH。

## 服务与配置如何判定

配置目录使用当前 `CLAUDE_CONFIG_DIR`；变量未设置时使用 `~/.claude`。目录不按机器绑定。

服务类型按照以下顺序判定：

1. `auth login` 和 `setup-token` 进入官方分支。
2. 启用 Bedrock、Vertex 或 Foundry 的环境开关，归入 `third-party`。
3. 非官方 `ANTHROPIC_BASE_URL` 匹配策略中的本机 Magpie 地址，归入 `magpie`；其他非官方地址归入 `third-party`。
4. API Key、Auth Token、认证头、`apiKeyHelper` 或 `forceLoginMethod=console`，归入 `api`。
5. 没有上述信号时，归入 `claudeai`。

直接使用 Anthropic 官方 API Key 也属于 `api`。服务类型来自配置推断；认证检查另外调用真实 Claude 的 `auth status --json`。

认证状态要求 `loggedIn: true` 且命令成功退出。Claude 会把配置的 API Key / Token 也视为认证来源，这不等于远端服务已经验证凭据有效。`hasCompletedOnboarding` 只标记完成引导，不能代替 API 配置或认证来源。

`auth login/status/logout`、`setup-token`、独立的帮助和版本命令允许在未登录时执行，避免无法进入认证流程。

## 官方会话的严格检查

官方分支会检查：

- 当前配置目录存在；`forceLoginMethod` 可缺省或为 `claudeai`。
- 没有其他认证来源覆盖。显式的 `CLAUDE_CODE_OAUTH_TOKEN` 也受此限制；官方会话使用配置目录中保存的登录。
- TLS 校验未被 `NODE_TLS_REJECT_UNAUTHORIZED=0` 关闭。
- HTTP(S) 代理已设置，所有已设置的代理变量均与策略一致。
- `NO_PROXY` / `no_proxy` 不会绕过官方域名。
- Claude 和 curl 可执行，且真实程序不会递归指向检查入口。
- 本地代理端口可连接。
- API 和登录域名的 trace 返回有效的 HTTPS/TLS 信息，出口 IP 与地区符合允许清单。
- 无凭据 GET 官方 API 返回预期的 401 或 405 错误响应。
- 网络检查成功后，真实 Claude 报告存在认证来源。

网络请求明确指定代理、启用 curl 的 TLS 校验、不读取 curlrc，不自动回退直连。三个官方探测并发执行，按策略对特定连接错误和 502/503/504 最多重试一次。

策略中的代理不会自动写进进程环境。可以在启动环境或已启用的 settings `env` 中设置代理。例如：

```sh
export HTTP_PROXY="http://127.0.0.1:7890"
export HTTPS_PROXY="$HTTP_PROXY"
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTP_PROXY"
export ALL_PROXY="$HTTP_PROXY"
export all_proxy="$HTTP_PROXY"
```

出口检查用于确认是否符合你自己的固定策略；它不保证账号权益、凭据有效性或模型生成能力。

## 第三方服务的可选检查

策略初始配置为：

```json
"optional_checks": {
  "magpie": [],
  "third-party": [],
  "api": []
}
```

空数组表示跳过该类服务的可选检查。认证状态仍会统一检查。

| 可选项 | 适用类别 | 行为 |
| --- | --- | --- |
| `tls` | 三种类别 | 检查是否设置了 `NODE_TLS_REJECT_UNAUTHORIZED=0`，不访问网络 |
| `magpie-gateway` | `magpie` | 请求策略指定网关的 `/v1/models`，要求 HTTP 200 和非空目录；使用本地占位 Token `magpie`，不检查具体模型是否存在 |

例如保持 Magpie 只检查认证，为其他 API 开启 TLS 配置检查：

```json
"optional_checks": {
  "magpie": [],
  "third-party": ["tls"],
  "api": ["tls"]
}
```

启用的检查失败会阻止启动。规则按服务类别配置，不会从项目 settings 中读取 `optional_checks`。

## 配置来源、认证隔离和 SDK

普通会话按“Shell 环境 → 用户配置 → 祖先与当前项目配置 → 临时 settings → 系统管理配置”合并检查输入。官方分支还会拒绝已启用项目配置对受保护账户、网络字段的修改。

认证命令只检查继承环境、选定配置目录的用户配置和系统管理配置，不扫描父项目目录，避免独立登录目录意外继承另一份用户配置。

```sh
claude --setting-sources '' --settings '{"disableDeepLinkRegistration":"disable"}' \
  -p --input-format stream-json --output-format stream-json --verbose
```

上述命令中的空字符串禁用 user/project/local 文件来源；禁用文件不会被读取或解析。临时 settings 可使用 JSON 对象或 JSON 文件，检查其格式、环境变量类型和认证相关字段。系统管理配置及其 fragments 仍参与检查并覆盖临时配置。

临时配置不能通过 `env.CLAUDE_CONFIG_DIR` 改变认证目录。TLS、代理和认证来源仍按实际生效配置检查；认证探测只接收配置参数，不读取 SDK 的 stdin。启动报告写入 stderr，stream-json 的 stdout 留给真实 Claude。

## 策略参考

完整结构见 [policy.example.json](policy.example.json)。

| 字段 | 含义 |
| --- | --- |
| `schema` | 当前为 `1` |
| `real_binary` | 真实 Claude 程序的绝对路径 |
| `curl_binary` | curl 的绝对路径 |
| `proxy` | 本机回环地址上的 HTTP 代理，官方分支强制匹配 |
| `expected_ips` | 允许的实际出口 IP 列表 |
| `allowed_countries` | 允许的两位大写地区代码 |
| `connect_timeout_seconds` / `timeout_seconds` | 连接与单次请求超时；后者最多 15 秒 |
| `attempts` | 每项官方探测的总尝试次数，`1` 或 `2` |
| `real_binary_by_hostname` | 可选，按机器选择真实 Claude 安装路径 |
| `magpie_url_by_hostname` | 可选，按机器标记本地 Magpie 地址；不会固定会话模式 |
| `optional_checks` | 按服务类别选择可选检查 |

`claude-guard --third-party` 是另一个独立的可选功能：仅在官方检查成功后请求代码中指定的外部风险评分服务，结果不决定放行。默认不查询该服务。

退出码：`0` 表示通过或允许执行；`77` 表示未认证；`78` 表示策略或配置错误；`69` 表示网络/服务检查失败，或无法确认认证状态；`130` 表示被用户中断。选中的第三方检查失败返回 `69`。

## 验证与开发

无需网络、账号或模型请求的测试：

```sh
python3 -B -m unittest discover -s tests -v
git diff --check
```

真实 SDK 初始化验证需可用的官方网络策略和已有登录目录，显式运行：

```sh
python3 tests/verify_sdk_initialize.py \
  --policy /absolute/path/to/policy.json \
  --binary /absolute/path/to/real/claude \
  --config-dir /absolute/path/to/authenticated/profile
```

该脚本使用 Magpie 0.1.2766 的参数结构，只发送 `control_request.initialize`，收到成功响应后结束进程，不发送用户消息。来源对应 [Magpie 的 worker 参数及协议](https://github.com/yetone/magpie/blob/c86fa2a341628ff3e12da15df2b9c4ed60821000/internal/gateway/claude_subscription.go)。它会联网执行官方检查，不加入默认 CI。

```text
preflight.py                   检查流程、参数与配置解析、报告和启动
bin/                           POSIX Shell 入口
install.py                     安装程序和初始示例策略
policy.example.json            无个人路径或凭据的配置示例
tests/test_*.py                隔离回归测试
tests/verify_sdk_initialize.py  显式运行的真实 SDK 协议验证
```

每次启动只检查当时的状态，不持续监控会话。检查器自行解析配置，真实 Claude 也会读取原始参数和文件；Claude 版本升级或配置在检查后发生改变，可能产生差异。普通会话保守扫描祖先目录，这也可能比 Claude 的项目发现更严格。

实际策略、登录目录、备份和日志不属于源码仓库。环境配置和凭据不会自动上传。

## License

[MIT](LICENSE)。本项目为独立工具。
