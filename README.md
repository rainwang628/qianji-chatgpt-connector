# 钱迹 × ChatGPT 个人账单助手 v0.4.0

更新日期：2026-11-14。用途：让个人 ChatGPT 查询钱迹，并在预览和确认后编辑已有账单；不提供新增或删除。

源码私有备份：[rainwang628/qianji-chatgpt-connector](https://github.com/rainwang628/qianji-chatgpt-connector)。
仅同步适配源码、测试、部署模板和说明；`.private/`、凭据、账单缓存、日志、运行环境及第三方 vendor 均不上传。

这是可部署的连接工具源码包。启动并接入后，ChatGPT 才能使用；ZIP 本身不是可直接上传安装的 ChatGPT 插件。
原始源码包交付时尚未绑定账号。本机现已完成钱迹账号绑定、真实账单核对、ChatGPT 网页及 iPhone 实际调用验证；
当前运行方式以 `MAC_DEPLOYMENT.md` 为准。部署与验证过程没有修改任何钱迹账单。

本机安装记录、原生 Bun 启动方式和当前验收状态见 [MAC_DEPLOYMENT.md](MAC_DEPLOYMENT.md)。

## 能做什么

| 需求 | 工具 |
| --- | --- |
| 绑定钱迹账号 | `connect_qianji`，生成一次性本机登录链接 |
| 查询账本、分类、标签和成员 | `list_books`、`list_categories`、`list_tags`、`list_book_members` |
| 查询账单及详情 | `list_bills`、`get_bills` |
| 按账本与日期汇总收支 | `get_bill_statistics` |
| 查看预算、资产与账号配置 | `get_budget`、`list_assets`、`get_user_info` |
| 同步其他设备的最新变化 | `refresh_cache` |
| 预览已有账单的修改 | `preview_bill_edit`，返回原值、新值及五分钟有效的预览编号，不保存 |
| 保存已经确认的修改 | `apply_bill_edit`，仅接受未使用且未过期的预览编号 |
| 预览和保存批量分类及受限账户修改 | `preview_batch_categories`、`apply_batch_categories`，每批不超过 10 笔 |

没有新增、删除账单或管理员工具，也不直接暴露上游 `update_bills`、`update_transfer`、`update_refund`。
白名单同时用于工具发现与执行，直接猜测未开放的工具名称也会被拒绝。
绑定账号与刷新本地缓存会改变服务的连接或缓存状态，但不会修改钱迹账单；其 MCP 注解不会将这两个动作冒充纯读取。

## 编辑已有账单

支持普通支出、收入、待报销支出、转账、信用卡还款和退款，具体可编辑字段沿用该类型的上游接口。
支持的字段包括金额、时间、备注、分类、资产、账本和标签，退款等类型的字段范围更小。
金额使用对应币种主单位，最多两位小数；时间必须包含时区。分类、资产等 ID 必须先查询，不得猜测。
不支持在收入与支出之间直接转换类型；其他特殊业务类型请在钱迹 App 中处理。

一次只编辑一笔：先查询账单并调用 `preview_bill_edit`，展示完整原记录、字段变更、账本和币种；
由用户确认本次具体变更后才调用 `apply_bill_edit`。保存会改变云端账单，也可能影响关联资产余额。
`apply_bill_edit` 标注为非只读、具有覆盖风险、不可自动重试的写入工具。ChatGPT 客户端承担操作批准，
不要选择“记住批准”；预览编号本身不证明用户已经批准。

服务端将预览内容固定在内存中，有效期五分钟，使用一次即失效，服务重启也会清空。
提交前再次同步并比对原账单，发现变化会拒绝覆盖；这不能消除远端设备恰在最后一刻并发修改的风险。
转账手续费与优惠互相清除的影响也列入预览。写入超时或返回错误时可能已经生效，必须刷新核对，不得自动重试。
本次真实验收只执行预览；实际保存路径使用虚构账单测试，没有改动用户的真实账单。

## 批量分类及账户修改

`preview_batch_categories` 接受最多 10 笔普通收入或支出的真实账单 ID 和目标分类 ID。
可选同时修改普通支出的账户：仅支持同一账本内同一对计入资产的人民币账户，账户须启用，
本位币为人民币，且账单没有优惠或跨币种换算；目标账户余额不足时拒绝。所有行须明确指定目标账户。
逐笔列出账本、原账户、新账户、币种、日期、金额、备注、原分类及新分类，账户迁移还展示账户余额的预计变化。
预览读取当前缓存，不主动刷新或写入财务账单；客户端必须向用户展示整个批次及余额影响。

用户确认具体批次后，调用 `apply_batch_categories`，原样传入预览返回的 `planId` 和
`reviewed`、`balancePreview`，由宿主为这一整个写入调用请求本次批准，不记住批准。保存前会刷新缓存，
逐笔比较完整账单、名称目录及相关余额；任何一项变动都会拒绝整个批次。预览五分钟有效且只能使用一次。
批量工具不支持金额、备注、转账、退款、新增或删除。

上游会一次发送多笔修改并回读，但不保证原子性。部分成功、超时、异常、进程中断或
返回信息不完整时，结果可能不确定；不要自动重试，应逐笔刷新核对。为恢复核对提供线索，
写入前后的批次摘要和结果保存在 `.private/batch-category-audit.jsonl`，按私人财务数据保护，
写入前若无法记录摘要则拒绝本次修改。账户缓存与实际远端并发修改之间仍存在竞争窗口，
不同桥接进程的锁不共享。禁止将审计日志纳入 Git 或公开发布。

v0.4.0 的账户迁移功能已通过隔离环境虚构账单测试及一次本机真实账单只读预览；
真实工具刷新、审批弹窗与批量保存尚未验证；
不能因此认定实际使用时一定只弹一次确认。

## 技术路线与限制

本工具适配社区项目 [fangzhengjin/qianji-mcp](https://github.com/fangzhengjin/qianji-mcp)，
核对的上游版本为 `74a8b17bc552b6ee168e1ca246e81b465322a258`。
钱迹协议、账号绑定、云端同步及统计由上游实现；本包提供 ChatGPT 侧的工具白名单、编辑预览、请求校验、凭据注入与私有连接入口。

上游依赖钱迹未公开的云端 HTTP 接口，属于社区实现，不能等同于钱迹官方授权的开放 API；钱迹升级或账号验证策略变化可能使连接失效。
钱迹官方 iOS 文档说明了快捷指令自动记账与带白名单的本机 URL Scheme 读取能力，这些本机接口本身不等于 ChatGPT 云端可直接调用的远程服务。

采用 OpenAI Secure MCP Tunnel 连接私有的 stdio 工具，不公开无鉴权公网接口。
需要你自己的 OpenAI Platform 隧道权限、运行密钥及可添加自定义 MCP 的 ChatGPT 环境；这些权限要在实际账号中确认。
服务运行的电脑或服务器必须在线。首次账号绑定请在部署服务的电脑浏览器中完成，绑定页的 `127.0.0.1` 地址不能直接在另一台手机打开。
若以后改成服务器部署，需要另行配置安全的绑定页访问方式；本包没有配置公网域名或公网 OAuth 服务。

## 在自己的电脑上启动

准备 Python 3.11 或更新版本、Docker Compose，以及可用的 Docker 引擎。
当前脚本按 macOS/Linux 编写。以下操作均在解压目录中执行，不需要把账号密码发给 ChatGPT。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python3 setup_local.py
docker compose up -d
.venv/bin/python qianji_chatgpt.py --check
.venv/bin/python qianji_chatgpt.py --login
```

`--check` 只证明服务和工具列表可访问，不证明钱迹账号已经登录。
`--login` 返回一次性登录链接；在同一台电脑浏览器中打开并输入自己的钱迹登录信息。
上游绑定页与缓存运行在你部署的服务中。上游会保存登录状态和账单缓存，PAT 在上游数据库中以明文保存；请把本地数据卷和备份作为私人财务资料保管。
完成绑定后，再真实调用 `list_books` 并与你的钱迹账本核对，才可认定账号读取成功。

初始化脚本将连接凭据写入 `.private/`，不在终端输出。重复运行不会轮换或覆盖已有凭据。
不要删除 `.private/` 后直接重新生成凭据并复用旧数据卷；如需轮换，须按上游文档同步处理。

如果容器镜像对应标签不可获取，请从上述固定上游提交自行构建；本次未下载或启动该镜像，不能把源码审阅视为镜像可用性验证。

## 接入 ChatGPT

按 [OpenAI 官方 Secure MCP Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
从 Platform 隧道设置取得 `tunnel_id`、下载当前 `tunnel-client`，并核实隧道与目标 ChatGPT 环境的关联和权限。
将运行密钥通过本机安全输入设置到 `CONTROL_PLANE_API_KEY` 环境变量，不要写入聊天或代码文件。

按实际解压路径替换下面的 `/absolute/path/qianji-chatgpt-connector`：

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile qianji-readonly \
  --tunnel-id YOUR_TUNNEL_ID \
  --mcp-command "/absolute/path/qianji-chatgpt-connector/.venv/bin/python /absolute/path/qianji-chatgpt-connector/qianji_chatgpt.py"

tunnel-client doctor --profile qianji-readonly --explain
tunnel-client run --profile qianji-readonly
```

使用 ChatGPT 网页中的 Plugins → 加号 → Add custom MCP server → Connection: Tunnel，
选择同一条隧道，将名字设为“钱迹账单助手”，创建并安装后在新对话中选择该插件。
桥接进程自身通过受限隧道提供服务，钱迹上游 PAT 由本机注入；不要将上游 PAT 当作 OpenAI 密钥。
保持隧道客户端及钱迹上游运行。本次 Mac 部署使用原生 Bun，网页端与用户的 iPhone 均已完成实际调用验证。

可以用这些请求验收：

- “用钱迹查询我的账本列表。”
- “先刷新钱迹数据，再查日常账本这个月的总收入和总支出。”
- “查这个月餐饮支出，说明查询的账本、币种和日期范围。”
- “把这笔已有账单的备注改为交通补贴，先预览，不保存。”
- “帮我记一笔午餐 35 元。”——本版应说明没有新增功能，不得声称已记账。

## 验证状态

`TEST_RESULTS.md` 记录本包在模拟上游上的自动化验收结果。测试只使用虚构数据。

本机部署已验证：账号登录、真实账本和账单读取、用户与钱迹 App 核对、私有隧道与 ChatGPT 插件调用；
iPhone 实际调用由用户操作并确认返回正确账单。Docker 路径未使用，未验证 Docker 镜像；未实际休眠或重启机器进行测试。
v0.2.0 的 33 项虚构数据测试通过，真实普通支出与转账的编辑预览、ChatGPT 网页实际预览通过；
验收后回读确认账单未变。真实账单保存及新版 iPhone 编辑流程尚未实测。
只有真实调用成功并核对结果后，才能说“已连接你的钱迹”。

## 开发与配置

```bash
.venv/bin/python -m unittest discover -s tests -v
```

默认上游：`http://127.0.0.1:3000/mcp`。可用 `QIANJI_UPSTREAM_URL` 覆盖；非回环地址强制 HTTPS。
默认 PAT 文件：本包 `.private/upstream.pat`。可用 `QIANJI_PAT_FILE` 指定其他私有文件。
设置 `QIANJI_READ_ONLY=1`（或 `true`/`yes`/`on`）时只注册查询类工具，`preview_bill_edit` 与 `apply_bill_edit` 既不列出也拒绝执行；`connect_qianji`、`refresh_cache` 仍保留，供登录续期与同步。适合只做查询的客户端（如 DeepSeek Harness）。
连接上游时不读取系统代理环境变量，也不跟随重定向；默认通过回环地址直接访问本机容器。
工具参数与输出沿用固定上游版本的 MCP Schema，并在每次调用时重新获取与校验。
工具超时不会自动重试；接口出错时报告失败，不输出原始异常或凭据。

## 资料

- [钱迹官方 URL Scheme](https://docs.qianjiapp.com/url-scheme/readme.html)
- [钱迹官方 iOS 自动记账说明](https://docs.qianjiapp.com/auto/qianji_auto_ios.html)
- [上游源码与 MIT 许可证](https://github.com/fangzhengjin/qianji-mcp)
- [OpenAI：添加自定义 MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OpenAI：Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

本包为独立适配代码，不包含钱迹 App，也未复制上游实现。上游镜像与 Python MCP SDK 各自遵循其许可证。
