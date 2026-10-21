# OssLD

> **Demo 版本**：功能和界面还在快速调整中。

收录 [LINUX DO](https://linux.do) 社区「开源推广」标签下的开源项目。作者用 LINUX DO 账号登录后，自己填写帖子链接、标题、分类、GitHub 仓库和发布日期提交，每个项目都链接回原帖。网站服务器不抓取 LINUX DO 的帖子内容。

线上地址：https://ossld.com

## 致谢

感谢 [LINUX DO](https://linux.do) 社区。项目信息由作者本人提交，只展示标题、作者、发布日期和 GitHub 仓库，不搬运正文和回复，讨论请回到原帖。

本项目是社区个人项目，非 LINUX DO 官方站点。

## 本地运行

需要 Python 3.12。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows 用 .venv\Scripts\pip
.venv/bin/uvicorn app.main:app --reload     # Windows 用 .venv\Scripts\uvicorn
```

打开 http://localhost:8000 。数据库和下载的封面图默认放在 `data/`，可以用环境变量 `DATABASE_PATH` 改数据库位置。

## 管理后台

打开 `/admin`，用服务器环境变量 `ADMIN_PASSWORD` 设的密码登录（不设就不开后台，密码请用一长串随机字符）。可以删除项目、封禁作者（封禁后不能再提交，可以勾选顺手删掉 TA 的全部项目）、解封。

## 管理员命令

```bash
python -m app.admin category <帖子编号> <分类>  # 改分类
python -m app.admin remove <帖子编号>...       # 删除
python -m app.admin images                    # 补下载缺失的封面图
```

## 协议

[MIT](LICENSE)
