# OssLD

> **Demo 版本**：功能和界面还在快速调整中。

收录 [LINUX DO](https://linux.do) 社区「开源推广」标签下的开源项目。贴上发布帖链接，自动解析出标题、作者、发帖时间和 GitHub 仓库，每个项目都链接回原帖。

线上地址：https://ossld.com

## 致谢

感谢 [LINUX DO](https://linux.do) 社区。项目数据全部来自 LINUX DO 的公开帖子，只展示标题、作者、发帖时间和 GitHub 仓库，不搬运正文和回复，讨论请回到原帖。

本项目是社区个人项目，非 LINUX DO 官方站点。

## 本地运行

需要 Python 3.12。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows 用 .venv\Scripts\pip
.venv/bin/uvicorn app.main:app --reload     # Windows 用 .venv\Scripts\uvicorn
```

打开 http://localhost:8000 。数据库和下载的图片默认放在 `data/`，可以用环境变量 `DATABASE_PATH` 改数据库位置。

## 管理员命令

```bash
python -m app.admin add [--category 分类] <帖子链接>...  # 直接收录，不走网页提交流程
python -m app.admin category <帖子编号> <分类>            # 改分类
python -m app.admin remove <帖子编号>...                 # 删除
python -m app.admin images                              # 补下载缺失的封面图和头像
```

## 协议

[MIT](LICENSE)
