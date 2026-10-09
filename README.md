# 长安大学通知监控与每日邮件推送系统

当前版本：**v1.1.0（可靠性版本）**

系统每天 21:00 动态抓取通知列表及详情，再汇总数据库中全部待发通知，调用 DeepSeek 生成摘要并通过 QQ 邮箱发送。没有待发通知时仅写日志。

## 设计说明

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
- 长安大学列表项是 JavaScript 可点击的 `<div>`；当前实现会逐条点击并记录稳定的 hash 路由 URL。门户 DOM 或路由规则改变后仍需更新选择器或点击逻辑。

## 开发检查

```powershell
pip install -r requirements-dev.txt
ruff check .
pytest
```

GitHub Actions 会在每次 push 和 pull request 时执行 Ruff、pytest 和字节码编译。
