# Mac 本机部署与验收

更新日期：2026-11-11。当前阶段：**已连接：钱迹真实查询、ChatGPT 网页及 iPhone 实际调用均已验证**。

用户已在原微信账号内完成邮箱绑定及密码设置，并在 Mac 的本机页面完成登录。
账本和 5 笔真实账单已由用户与钱迹 App 核对一致；只向聊天返回了少量核对样本。
最初查询只取最近 3 笔，用户指出的另外 2 笔通过精确金额筛选补查，均在最初样本的时间范围之前，未发现同步差异。
ChatGPT 网页普通 Chat 已通过实际插件调用读回同一笔经核对的账单；当时 v0.1.0 只提供查询。
用户在同一账号的 iPhone ChatGPT App 中完成实际插件调用，并确认返回正确账单。
网页验收对话：<https://chatgpt.com/c/6af41813-9704-83ec-bb09-5a693e51e0d4>。
本记录不保存财务明细、邮箱、密码或运行密钥。

## 安装位置与环境

项目目录：`~/rain/projs/services/qianji-chatgpt-connector`。没有修改 rain-docs 或其他项目。

Mac 为 arm64、macOS 27.2。原系统 Python 是 3.9.6，未安装 Docker。
本次使用上游明确支持的原生 Bun 方式，运行同一个固定提交的源码；Python 适配层沿用查询实现并增加受限编辑。

| 组件 | 安装版本 | 项目内位置 |
| --- | --- | --- |
| Python | 3.12.14 | `.runtime/python/`、`.venv/` |
| Python MCP SDK | 1.29.0 | `.venv/` |
| uv | 0.13.7 | `.runtime/uv/` |
| Bun | 1.3.14 | `.runtime/bun/` |
| OpenAI tunnel-client | 0.0.20 | `.runtime/tunnel/` |
| qianji-mcp | `74a8b17bc552b6ee168e1ca246e81b465322a258` | `vendor/qianji-mcp/` |

上游源码和运行包的来源及 SHA-256 见 `DEPLOYMENT_SOURCES.json`。
原始源码包和软件运行包保留在 `~/rain/softs/`。
Python 依赖快照见 `requirements-macos.lock`；上游依赖使用原始 `bun.lock`，安装时启用 frozen lockfile 并禁用安装脚本。

## 已观察到的检查结果

- ChatGPT 网页当前登录 Plus 账号；Plugins 的 Add custom MCP server 表单可以选择 Tunnel。
- Platform 的 Personal 组织可以进入创建隧道表单，并显示可关联的 ChatGPT 工作区。
- Mac 在沙箱外可通过 TLS 访问 OpenAI API、PyPI、GitHub 和钱迹 API。此检查不代表已有隧道调用权限。
- 原有 16 项测试及新增编辑测试共 33 项虚构数据测试通过；测试需要允许监听回环端口，在 Codex 沙箱内会被系统拒绝。
- 固定上游源码的 TypeScript 检查通过。
- 本机服务由当前登录会话的 launchd 管理，监听地址确认为 `127.0.0.1:3000`；未提供 PAT 的 `/mcp` 请求返回 401。
- 实际上游工具发现已通过，v0.2.0 桥接层返回 12 个原有工具及 2 个本地编辑工具，共 14 个。

私有隧道已创建，关联当前 Personal Platform 组织和同账号的 ChatGPT 工作区。
运行密钥由用户通过本机隐藏输入存入 macOS 钥匙串，权限仅为 Tunnels Read + Use，有效期 30 天。
`tunnel-client doctor` 配置检查通过；运行后 `/healthz`、`/readyz` 均返回 200，成功取得隧道元数据并转发 ChatGPT 请求。
插件现名“钱迹账单助手”（原名“钱迹只读查询”），已安装在个人账号中：[插件页](https://chatgpt.com/plugins/plugin_asdk_app_6af4176629088191b431a9492ba45f85)。
网页端和 iPhone 的实际查询验收均已完成。一键启动脚本已实际复用当前两个运行中的服务，鉴权和隧道就绪检查均通过。
没有为了验证而让 Mac 休眠或重启，因此未把断电、休眠恢复和重启过程标为实测。

## v0.2.0 编辑范围与验收

用户确认仅供个人使用，并明确同意启用已有账单编辑。新增、删除和管理员工具保持关闭。
`preview_bill_edit` 先刷新并读取一笔已有账单，按类型选择固定上游的普通账单、转账/还款或退款修改接口。
预览只保存在桥接进程内存中，五分钟过期，重启失效；`apply_bill_edit` 仅能消费该预览一次，提交前再次同步并核对原记录。
保存工具标注为非只读和有覆盖风险，客户端仍需批准；不要记住写入批准。预览编号不能替代用户批准。
上游写入失败或响应丢失时可能已经生效，服务不会自动重试，必须刷新后核对。

33 项虚构数据测试覆盖完整 stdio 预览与保存、并发/重复提交、过期、原记录变化、字段校验和写入结果不确定。
本机真实普通支出与转账的备注预览均成功，返回 `saved=false`；未调用真实保存接口，未改动用户账单。
用户再次确认启用后，ChatGPT 工具列表刷新完成，界面显示 11 个 Read 和 3 个 Write（后者含连接维护与刷新缓存）。
ChatGPT 网页实际调用 `preview_bill_edit`，正确返回原备注与预览值、`saved=false`，调用轨迹显示 Previewed Bill Remark Edit。
验收对话：<https://chatgpt.com/c/6af421d0-4fb0-83e8-8525-15782aa250e9>。
随后本机回读确认原金额与备注未变，并重启隧道清空测试预览；不能使用验收对话中的旧 planId 保存。
真实写入兼容性、退款实账编辑和新版 iPhone 编辑流程仍未实测。
`readonly` 字样保留在旧隧道配置、钥匙串服务名和 launchd 标签中，避免更换已有凭据关联，不代表 v0.2.0 仍然只读。

## 日常启动与停止

在项目目录执行：

```sh
./start.command
.venv/bin/python macos/service.py status
.venv/bin/python macos/service.py status --component tunnel
.venv/bin/python qianji_chatgpt.py --check
./stop.command
```

也可以在 Finder 中双击 `start.command` 或 `stop.command`。
`start.command` 顺序启动上游和已配置的隧道；`stop.command` 顺序停止隧道与上游。
服务标签为 `com.rain.qianji-readonly.upstream` 和 `com.rain.qianji-readonly.tunnel`，本机配置在 `.private/`，日志在 `logs/`。
`status` 中的 `checks_account_data` 固定为 false：该命令只检查本机进程、HTTP 鉴权或隧道就绪，不重新查询个人账单。
沙箱阻止本机 HTTP 探测时，`probe_unavailable` 为 true，不能据此认定服务失败或缺少鉴权。

当前只配置了本次 macOS 登录会话内的进程监督。**尚未安装重启后自动启动项**。
重启并登录 macOS 后，确保网络可用，双击 `start.command` 即可启动两部分。
钥匙串随登录解锁后，隧道运行密钥自动加载；如果系统显示钥匙串访问提示，由用户在本机处理。

## 运行密钥续期

本次运行密钥有效期 30 天，约在 2026-12-11 到期，精确失效时间以 Platform 为准。
到期后隧道调用会失败，需创建新的 Tunnels Read + Use 受限运行密钥，再双击 `store-tunnel-key.command` 在隐藏输入中保存。
随后执行下面两项重启隧道，让进程加载新密钥：

```sh
./stop-tunnel.command
./start-tunnel.command
```

钥匙串条目：service 为 `rain.qianji-readonly.openai-tunnel`，name 为 `runtime-key`。
密钥不写入命令参数、profile、Git 或日志；隧道 profile 只保存环境变量引用。
此私有隧道由 OpenAI 的组织/工作区权限和运行密钥保护，插件未额外部署公网 OAuth 服务。
钱迹上游继续强制 PAT 鉴权，且只监听回环地址；没有开放无鉴权公网财务接口。

## 钱迹登录

```sh
.venv/bin/python macos/prepare_login.py
```

该命令把一次性票据保存在 `.private/login.html`，只输出文件位置和失效时间，不打印票据。
在本机 Chrome 中打开这个本地 HTML 文件，会跳转到 `127.0.0.1:3000/connect`，并从地址栏移除票据。
用户自行输入账号、密码和时区，密码在浏览器中转换为摘要。明文密码和密码摘要不保存。
登录后，上游会同步账单到本机 SQLite 缓存；首轮核对只向聊天返回少量账单。
页面显示登录成功后，仍需真实调用并与钱迹 App 核对。

PAT、钱迹登录令牌和账单缓存在 `.private/` 中，目录权限为 700，凭据文件为 600；该目录不进入 Git。
上游数据库中 PAT 为明文，原方案的这一限制仍然存在。请按私人财务资料保管该目录和它的备份。

## 休眠、合盖和重启

电脑必须联网且保持唤醒，本机上游与 OpenAI 隧道客户端也必须同时运行。
熄屏本身不要求停止服务；实际进入休眠后，隧道会中断，重新唤醒且网络恢复后才可能重连。
初期建议插电并开盖使用，合盖通常会使 Mac 进入休眠。本次未修改系统休眠设置。

当前电源配置报告 AC Power 下 `sleep=1`、`displaysleep=10`。在需要临时保持开盖运行时，可在另一个终端执行：

```sh
caffeinate -i
```

按 Ctrl-C 结束。该命令不保证合盖后继续运行，也不会让关机或重启期间的服务可用。
iPhone 的查询已由用户实际验证。新增编辑工具需在刷新后的新对话中选择“钱迹账单助手”，新版编辑流程尚未在 iPhone 实测。

## 官方参考

- [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [ChatGPT 添加自定义 MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [ChatGPT 插件支持的客户端](https://learn.chatgpt.com/docs/plugins)
- [固定上游提交及原生运行说明](https://github.com/fangzhengjin/qianji-mcp/tree/74a8b17bc552b6ee168e1ca246e81b465322a258)
