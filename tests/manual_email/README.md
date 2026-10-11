# 手动邮件联调

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe main.py send-test
```

默认从数据库选择最新的带附件通知；没有带附件通知时选择最新一条。
指定记录：`main.py send-test --notice-id 123`。

此命令会调用 DeepSeek 并通过 QQ SMTP 向 `.env` 的 `MY_EMAIL` 真实发信，
主题带“【测试邮件】”。每次执行都会再次发送，可能产生 API 费用。
不访问学校门户，不修改通知或正式投递记录。
附件仅使用已下载的本地文件，缺失或超限时按正式邮件逻辑展示下载链接，
不会重新下载。因此不能用此命令验收实时门户抓取或附件下载。

`test_sender.py` 是使用假摘要和假 SMTP 的自动化测试，pytest 不发真实邮件。
