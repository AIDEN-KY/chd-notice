# 长安大学通知监控与每日邮件推送系统

当前版本：**v1.1.0（可靠性版本）**

系统每天 21:00 动态抓取通知列表及详情，再汇总数据库中全部待发通知，调用 DeepSeek 生成摘要并通过 QQ 邮箱发送。没有待发通知时仅写日志。

## 设计说明

### 多人升级开发中（P0-01 / P0-02）

users、BCC、逐收件人批次与部分拒收记录已实现，但跨进程保护尚未验收。
正式 `send-now` / `send-pending` / `scheduler` 日报发送保持关闭：
没有 active 用户时跳过；有用户时也被发布保护阻止，不调用摘要、不标记通知已发送。
抓取仍可执行，故障告警仍只发管理员。不要自行开启
`multi_user_delivery_ready`；待后续安全任务完成再开放。

本地管理（不会发邮件）：

```powershell
.\.venv\Scripts\python.exe main.py users --action add --email student@example.com --consent-source "本人明确同意"
.\.venv\Scripts\python.exe main.py users --action list
.\.venv\Scripts\python.exe main.py users --action pause --user-id 1
.\.venv\Scripts\python.exe main.py users --action resume --user-id 1
.\.venv\Scripts\python.exe main.py users --action unsubscribe --user-id 1
```

退订后恢复还需 `--consent-source` 记录新同意。重复添加不会恢复停用状态。
不会自动导入 MY_EMAIL；确需导入管理员邮箱时使用
`users --action import-self --consent-source "本人明确同意"`。
邮箱只支持单个 ASCII 地址，保留 local-part 大小写、域名转小写，
不合并加号 / 点号别名。用户列表默认只显示 ID、状态和历史起点。
新用户默认只接收加入后新增的通知（按通知 ID 起点）；已有通知的新修订
也受此起点约束。已接受、永久拒收或未知的修订不会自动重发。
明确临时拒收最多尝试 3 次，补发只包含对应用户，使用原批次正文和摘要快照。
超时 / 结果缺失进入 unknown，等待后续恢复工具核对；SMTP 接受不等于收件箱送达。
当前不提供自动处理 unknown 或重置永久拒收状态的命令。
只有管理员收到明确历史补发请求后，才执行以下本地授权命令：

```powershell
.\.venv\Scripts\python.exe main.py users --action backfill --user-id 1 --consent-source "本人明确请求历史通知"
```

它保存授权记录，开放该用户历史起点，不清除已接受或未知投递事实；
不会立即发信，也不能解除正式发送保护。
暂停 / 退订者尚未发送的批次在发送前被标为 skipped；恢复后默认不补这些跳过项，
若本人明确需要，使用 backfill。重新订阅需新同意，历史起点重新建立。
多人待发选择使用 user_notice_deliveries，不依赖旧 notices.sent；
旧 mail_deliveries 保留且不会被当作新用户的送达事实。
手动 `send-test` 仍只发 MY_EMAIL；该地址若在 users 中已暂停或退订，则拒绝测试发送。
邮件链接仍不是自动退订网页，CLI 退订与网页闭环不可混淆。

- 每次发送查询数据库中全部历史 `sent=0` 通知，发送失败后跨天仍会继续处理。
- 详情 URL 是唯一键；正文和附件元数据使用内容哈希检测修改，修改后重新加入待发队列。
- 附件可下载且总原始大小不超过 20 MiB 时全部随信附上；总大小超过阈值时不附文件，正文列出所有附件链接。下载失败的附件也保留原链接。
- 抓取失败不会阻塞已有数据发送；摘要失败自动使用标题。
- 门户导航和 DeepSeek 临时故障最多重试 3 次；SMTP 不盲目重试，避免邮件实际送达后因响应丢失而重复投递。
- 动态抓取通知列表：连续 3 个全部已知页面后停止，最多 20 页；日志写入 `logs/chd-notice.log`，按天轮转并保留 30 天。
- 每次邮件投递记录状态、尝试次数和失败原因；CAS 过期、抓取失败或日报任务失败时发送故障告警。
- 选择器是站点最容易变化的部分，全部集中在 `config.py`，也能通过 `.env` 覆盖。

## 安装

建议使用 Python 3.11+：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
```

编辑 `.env`：

- `DEEPSEEK_API_KEY`：DeepSeek API Key
- `QQ_EMAIL`：QQ 发件邮箱
- `QQ_SMTP_AUTH_CODE`：QQ 邮箱设置中生成的 SMTP 授权码（不是邮箱密码）
- `MY_EMAIL`：测试收件邮箱

## 首次登录

```powershell
python main.py init-auth
```

程序会打开 Chromium 并进入通知页面。手动完成 CAS 登录，确认已看到通知列表后，回到终端按 Enter。登录状态保存在被 Git 忽略的 `auth.json`。

## 运行与测试

```powershell
# 只检查本地配置，不联网、不抓取、不发邮件
python main.py doctor

# 查看版本
python main.py --version

# 只抓取一次
python main.py scrape

# 立即执行“抓取 + 摘要 + 邮件”完整流程
python main.py send-now

# 常驻运行，每天 21:00 执行一次
python main.py scheduler
```

生产环境推荐让 Windows 任务计划程序每天 21:00 执行项目内的 `run_daily.bat`；无需让终端常驻。也可使用 `python main.py scheduler` 前台运行。系统本身没有每 30 分钟抓取任务。

## 首次联调选择器

学校通知页是 SPA，未登录时无法可靠获得实际 DOM。若日志提示找不到列表或详情，请在浏览器开发者工具中确认 CSS 选择器，并在 `.env` 覆盖。例如：

```dotenv
LIST_ITEM_SELECTORS=.actual-notice-row
TITLE_SELECTORS=.actual-title
TIME_SELECTORS=.actual-time
DEPARTMENT_SELECTORS=.actual-department
DETAIL_LINK_SELECTORS=a.actual-detail-link[href]
DETAIL_CONTENT_SELECTORS=.actual-article-body
ATTACHMENT_SELECTORS=.actual-attachments a[href]
NEXT_PAGE_SELECTORS=button.actual-next
```

多个候选使用 `||` 分隔。修改后先执行 `python main.py scrape` 检查日志和 `notices.db`。

## 已知边界

- QQ SMTP 与 SQLite 不支持共同事务：邮件成功投递后、数据库标记前若进程崩溃，下次仍可能重复发送。v1.1.0 会保存稳定 Message-ID、投递状态和尝试次数以便追踪，但普通 SMTP 无法提供严格的“仅投递一次”保证。
- `auth.json` 含登录会话凭据，必须像密码一样保护。过期时程序打印“会话已过期，请重新手动登录”，重新执行 `init-auth` 即可。
- 长安大学列表通过登录会话中的 Playwright APIRequestContext 调用 `getGGInfo.do`，以 `PAGENUM/PAGESIZE` 动态分页。由 `GGDM` 构造详情 hash 路由，避免点击列表改变已读排序。详情仍用浏览器提取；接口或 DOM 改版时需调整集中配置。
- 已有通知按详情 URL 中的 `dm` 复用原记录，兼容旧版截断的栏目参数。完整部门及发布时间补全可能使旧记录哈希变化，重新进入待发队列。
- 升级后会发送全部历史待发通知，首次动态扫描可能形成较大的积压邮件。连续已知页面和页数上限是有限扫描策略，不保证检测所有更早通知的修改；达到上限时日志会明确提示。
- 详情正文容器需要实际填入 HTML 或出现附件链接才算加载完成，避免把 SPA 空壳误存为内容更新。

## 开发检查

```powershell
pip install -r requirements-dev.txt
ruff check .
pytest
```

GitHub Actions 会在每次 push 和 pull request 时执行 Ruff、pytest 和字节码编译。

### 分页诊断

如需适配门户分页控件，运行：

```powershell
.\.venv\Scripts\python.exe main.py diagnose-pagination
```

程序会打开有头 Chromium。确认通知列表完整显示后回到终端按 Enter，程序只读取
分页结构并输出 JSON，不点击通知、不写数据库，也不发送邮件。

如需定位门户列表接口，可运行 `python main.py diagnose-api`。确认列表显示后按
Enter，程序会把不含 Cookie/请求头的接口摘要保存到本地
`portal-api-diagnostics.json`；该文件已被 Git 忽略。
# 投递中断核对（P0-03）

每日任务、手动发送、抓取和 CAS 登录使用数据库旁的同一 `.task.lock` OS 文件锁。
冲突时命令不执行，命令行退出码为 2；定时任务记录警告并跳过本次。
锁文件保留是正常现象，进程退出后 OS 自动释放锁，不要删除锁文件解除占用。
这只支持单机部署，不能将数据库和锁文件放到网络共享盘供多台机器同时使用。

获得锁后恢复遗留的 `sending` 为 `unknown`；不知道 SMTP 是否接受的邮件不自动重发。
管理员可以列出未知投递（不显示邮箱），人工核对后选择确认或重试：

```powershell
.\.venv\Scripts\python.exe main.py deliveries --action list
.\.venv\Scripts\python.exe main.py deliveries --action confirm --batch-id 1 --user-id 2 --reason "已核对收件箱"
.\.venv\Scripts\python.exe main.py deliveries --action retry --batch-id 1 --user-id 2 --reason "已核对未收到，接受可能重复风险"
```

请替换真实批次/用户 ID，不要直接照抄确认。核对命令不发信；重试保留原快照与尝试次数，
下一次发送任务才执行，三次上限不被重置。处理原因保存在本地审计表，勿填敏感信息。
确认仅针对原通知修订，不会将之后修改的通知误标为已发送。
正式多人投递开关仍关闭，等待后续授权、网络和上线验收。

## 任务健康与会话恢复（P1-01）

每日任务的开始、结束、成功/降级/失败状态存入 `task_runs`；进程中断留下的 running
会在下一次持锁运行时标记 interrupted。`doctor` 可显示最近任务和最近成功抓取时间，
但不联网验证 CAS 是否仍有效，也不是独立的无人值守监控服务。

`send-now` 退出码：0=正常完成（可能无通知），3=抓取失败但库存处理完成，
2=任务锁冲突，未执行；未处理的任务异常为 1。定时器记录降级状态而不退出。
SMTP 接受不代表收件箱已送达；库存部分拒收等详情仍以逐收件人记录为准。

管理员相同类型故障告警默认冷却 3600 秒（config.py），限频状态及失败类型保存在
`admin_alerts`，不向订阅者发送系统告警。失败尝试也冷却，避免 SMTP 故障造成邮件风暴。
如果数据库无法保存限频状态，不发送无法限频的告警，只记录本地错误；数据库或 SMTP
不可用时无法保证远程通知，本版本未配置独立 webhook/外部心跳。

CAS 仍由你手动登录。初始化登录必须验证到通知列表再保存；初始化和正常抓取更新会话均
使用同目录临时文件、fsync 和原子替换，失败保留旧 `auth.json`。不要公开会话文件。
真实 CAS 恢复及 Windows 任务计划退出码仍需你手动验收，离线测试不能替代。

## 附件与邮件预算（P1-02）

附件下载使用 HTTPX 流式传输，Cookie 从 Playwright 当前会话按目标 URL 选取；
不再使用会先完整缓冲的 APIRequestContext 附件响应。每跳只允许 HTTPS、443 和
`ATTACHMENT_ALLOWED_HOSTS` 的精确主机名，最多三次重定向，不允许 IP 直连或 URL 用户名。
默认仅允许 portal.chd.edu.cn；真实附件若使用其他学校主机，须由你确认后加入名单，
不要为消除报错加入通配符或未知主机。

`MAX_SINGLE_ATTACHMENT_BYTES` 默认 20 MiB。检查声明大小，也对实际流累计计数，
超限立即停止、清理临时文件；SHA256 内容寻址及原子落盘继续保留旧版本。
请求 identity 编码，压缩响应拒绝下载；CAS 登录表单特征会被拦截，普通 HTML 附件仍可用。
登录检测是有限特征检查，不保证识别所有伪装页面。可信主机名单不是 DNS 绑定/重绑定防护，
仍依赖受信任学校域名、DNS 与 TLS；这不是供任意外部用户提交 URL 的通用下载代理。
关闭环境代理继承，使用标准 TLS 验证；需要学校代理、客户端证书或额外浏览器认证机制的
部署须单独验收，不能通过关闭 TLS 校验绕过。

原始附件总量限制仍为 20 MiB。`MAX_MESSAGE_BYTES` 默认也为 20 MiB，是本项目的保守
序列化 MIME 预算，不代表 QQ 官方上限。编码后超限则移除全部附件并重建链接邮件，
纯文本和 HTML 均列出对应下载链接；链接邮件仍超限就报错，不连接 SMTP。
下载失败只保留原链接，日志不打印 Cookie 或完整下载 URL。链接可能需要学校登录，
不公开镜像受限附件。真实学校附件和 QQ 接收情况仍由你验收。

## 详情失败补偿（P1-03 首批）

详情超时、HTTPX 传输异常和附件未完整下载会按 retry_attempts 有限重试，耗尽后记入
scrape_failures 并继续其他通知。CAS 过期及结构类 RuntimeError 仍终止抓取，不静默跳过。
下一次任务可补偿列表窗口外的 pending 失败（每次最多 20 条）；同次已处理 URL 不重复请求。
累计连续失败五轮进入 exhausted，不再通过补偿队列自动重试，记录保留供管理员检查；
若再次出现在最新列表中，仍允许检查。此处还没有人工重置队列的 CLI。

scrape_runs 记录开始/结束、页数、变更数和失败数，部分失败显示 partial。
scrape 命令部分失败返回 3；每日任务告警仅给管理员，库存仍可处理但健康状态为 degraded。
附件复查后才计算通知哈希，失败保留整条旧通知；同 URL 文件字节变化能生成新修订。
旧哈希算法升级但内容未变时仅更新基线，不重置已发送状态。

真实扫描覆盖周期仍未验收。本批不代表 P1-03 全部验收完成；
附件复查仅覆盖本次详情扫描及失败补偿触及的通知，不保证全库历史附件都已检查。

### 历史轮转增量

现已实现持久化 history_scan_state 和逐页覆盖记录 scrape_run_pages。
默认 HISTORY_SCAN_ENABLED=true、HISTORY_PAGES_PER_RUN=2；最新扫描和历史扫描共用
max_pages_to_scrape 的总逻辑列表页预算（默认 20），至少为最新扫描保留一页。
当总预算仅一页时不轮转。网络重试可产生额外 HTTP 请求，页预算不是总请求/时间预算。

最新扫描仍可在连续已知页停止；余下历史预算从游标续扫，不因已知 URL 提前停止。
完整处理一页后才推进游标，系统故障不会推进未完成页；临时失败进入补偿队列。
历史空页或不足一页时游标回到第二页；最新范围已处理过的页码可跳过，详情按 URL 去重。
历史预算至少两页时，每轮保留一页边界重叠；预算一页时直接推进，否则无法前进。
关闭历史开关会保留已有游标与失败记录，不删除数据。

十页固定假站点以一页历史预算能在十次运行内发现第十页修改，这不是实际门户的周期承诺。
真实覆盖周期依赖页数、增删/重排速度、预算及成功执行次数；不能从到过某页推断已抓全历史。
明确的实际覆盖周期验收仍待完成，P1-03 尚未全部关闭。

### 正文资源变化规则

内容哈希现包含按 DOM 顺序出现的正文链接 href、图片 src/srcset/alt，以及 picture 内
source 的 srcset。相对地址按通知详情 URL 解析；CSS、class、宽高等排版属性继续忽略。
普通 srcset 候选逐个解析地址和描述符；含 data URL 的 srcset 保守保留原值，
不按逗号拆分。复杂非标准 srcset 可能产生保守的变化判断，不保证所有浏览器选图语义等价。
本项不下载正文图片，因此同图片 URL 后面的字节变化不在检测范围；附件字节复查另行处理。

哈希算法升级会以数据库已存正文核对资源：没有真实变化只升级基线，保留 sent 和修订号；
链接或图片地址确实改变才生成新修订。旧快照本身未保存的信息无法追溯补测。

### 摘要缓存与日报积压预算

成功摘要存入 SQLite `summary_cache`，按完整标题/正文、模型及提示词版本建立
哈希键；跨任务及收件人共享。AI 关闭或命中排除策略时先返回标题，不读取缓存；
请求失败的降级结果不缓存。提示词语义调整时须同步更新 `summary_prompt_version`。
缓存含校内摘要，按数据库的敏感数据规则保护；当前发送任务串行，不承诺并发请求合并。

`MAX_NOTICES_PER_DIGEST=20` 限制新建批次的通知数，剩余通知顺延。
`MAX_DIGEST_MESSAGES_PER_DAY=1` 按上海时区、每名收件人持久化限制每日新日报数；
需要多封时管理员显式提高此值。发送前在同一事务中占用预算，结果未知也保守占用，
原固定批次的有限拒收重试不另占新日报预算。每日预算不是 SMTP 尝试次数上限。
部署前已经冻结的批次不会拆分或改写；如其超出新数量上限，需要管理员核对处理。
完整每日任务共享 `TASK_TIME_BUDGET_SECONDS`（默认 900 秒）的单调时钟截止时间。
抓取或摘要等待超时会取消并清理，未发送通知保持待发；每日任务记录 degraded，退出码 3。
已有成功投递记录不会回退。SMTP 启动后不强行取消线程，仍等待其结果并如实记录，
因此这不是“进程恰好在 900 秒终止”的硬实时保证；同步文件/数据库操作及资源清理也可能延长退出。
`send-pending` 单独运行也有预算，停止原因写日志；其现有 CLI 返回方式保持兼容。

`SUMMARY_CONCURRENCY` 默认 2，硬上限 8，每批次只创建有限 worker，不按积压规模
无界创建请求。同内容、模型及提示词版本的并发请求合并；429/服务端错误有限退避重试，
共享任务预算耗尽会取消等待及退避。全部摘要就绪并且预算未耗尽才领取收件人及发送。

摘要纯文本校验默认至少 50、最多 400 个非空白字符（50–80 字仍是提示词目标）。
空白、非字符串、HTML/代码块、控制字符及超范围输出不入缓存，降级并记录稳定 reason。
对明确标注的时间、截止、地点、QQ群、联系人、电话、邮箱，以及可识别的日期/号码，
从完整原文提取；遗漏时追加原文重要信息，允许超过摘要长度，不通过截断省略关键数字。
无法可靠识别无标签地点或隐含行动要求，不宣称验证了模型全部语义或不存在幻觉。
AI 关闭/排除策略继续优先，不外发正文；日志仅记录原因和错误类型，不记录密钥或响应正文。
提示词版本已更新，旧缓存仍保留但不会因策略升级直接复用。

### 只读积压预览

```powershell
.\.venv\Scripts\python.exe main.py preview
```

输出 JSON：当前上海日期、启用订阅人数、待发通知/收件人通知数、已有待发批次、
未知投递数、仍有每日预算的用户数、数量/大小预算、积压入库日期范围及历史政策。
不显示邮箱或通知正文，不请求门户、DeepSeek、SMTP；不创建日志、初始化/迁移数据库，
不领取批次或改变投递状态。SQLite 以 `mode=ro` 打开并读取同一事务快照。
`missing` 表示数据库不存在；`unreadable_or_incompatible` 表示不可读或结构不兼容，
预览不会尝试修复。`ready` 不等于允许正式发送，另看 `delivery_enabled`。

附件估算是全部待发修订的已存原始大小，不读取附件文件，不含 MIME 编码开销，
也不是今日邮件大小；未知大小单独计数。待发总数包含预算延期的通知，不能据此推断
今天会发送多少；未知结果不列入自动待发，已有冻结快照与新修订分别保留。

### 学校 PDF 预览附件

学校已知 `/qljfwapp/sys/emapcomponent/pdf/index.do?file=...` 链接是预览包装页。
下载时只解码一次 `file` 参数，只接受同站 HTTPS、指定 `getAttachmentFile/<hex>.do`
下载路径；外站、登录页、重复/空参数、非法字符和其他路径均拒绝。
真实下载地址用来选择会话 Cookie，后续重定向、流式大小及登录页面检查继续生效。
数据库和邮件仍保留原始附件链接、附件名称，下载结果继续记录大小和 SHA256。
相关主机及路径集中在 config.py；本次不放宽附件主机允许列表。

修复后重跑 `main.py scrape` 可重新处理遇到的失败通知；补偿队列也会有限重试。
这不是对门户连接中断的修复，真实附件是否恢复需用户手动抓取验收。

### 备份、只读诊断与 Windows 运维

`doctor` 不再初始化或迁移数据库，不创建下载/日志目录；只检查配置和本地条件。
`init-db` 用于明确初始化/增量迁移，升级前先备份；schema 版本独立记录为 1，
拒绝对未来版本降级。非法邮箱、调度时间、端口、重试次数及并发范围会被拒绝，
敏感配置错误不显示填写值。

```powershell
.\.venv\Scripts\python.exe main.py backup --output "snapshot-unique-name.db"
.\.venv\Scripts\python.exe main.py restore --input "snapshot-unique-name.db" --output "rehearsal-unique-name.db"
```

备份含个人数据，只选本地受保护目录。目标父目录须存在，目标文件不得存在；
restore 只做新文件恢复演练，不覆盖运行数据库，不发送邮件。复制使用 SQLite 在线备份 API，
包含已提交 WAL 数据，验证完整性和 schema；保留 accepted/unknown 投递事实。
Windows NTFS 权限、每天 21:00 任务计划、关机/睡眠补跑边界及实际回切注意事项见
[运维说明](docs/operations.md)。CI 已配置 Ubuntu/Windows 和 Python 3.11/3.13 矩阵，
含格式检查；远端 CI 结果需要另行核验。恢复演练不等于正式回切验收。
