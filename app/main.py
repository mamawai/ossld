"""网站：首页列表和提交页。

本地启动：.venv\\Scripts\\uvicorn app.main:app --reload
"""

import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import projects
from .linuxdo import Topic, TopicError, TopicRetryError, fetch_topic, parse_topic, parse_topic_id

HERE = Path(__file__).parent
BEIJING = timezone(timedelta(hours=8))  # 中国没有夏令时，固定 +8 就行
PREVIEW_TTL = 600  # 预览结果保留 10 分钟

projects.init_db()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # 不对外暴露接口文档
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/covers", StaticFiles(directory=projects.COVERS_DIR), name="covers")
app.mount("/avatars", StaticFiles(directory=projects.AVATARS_DIR), name="avatars")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["bjtime"] = lambda dt: dt.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
templates.env.filters["bjdate"] = lambda dt: dt.astimezone(BEIJING).strftime("%Y-%m-%d")
templates.env.filters["repo_name"] = lambda url: url.removeprefix("https://github.com/")
templates.env.filters["clean_title"] = projects.display_title
templates.env.globals["required_tag"] = projects.REQUIRED_TAG

# 预览时解析好的帖子先放内存里，确认收录时直接用，不再请求一次 linux.do。
# 只在单进程下有效，部署时 uvicorn 只开一个 worker
_previews: dict[int, tuple[Topic, float]] = {}


def _get_preview(topic_id: int) -> Topic | None:
    item = _previews.get(topic_id)
    if item and time.monotonic() - item[1] < PREVIEW_TTL:
        return item[0]
    return None


def _save_preview(topic: Topic) -> None:
    now = time.monotonic()
    # 顺手清掉过期的，免得内存越攒越多
    for k, (_, t) in list(_previews.items()):
        if now - t >= PREVIEW_TTL:
            _previews.pop(k, None)
    _previews[topic.id] = (topic, now)


def _notice(e: Exception) -> dict:
    """异常 → 提示框。红色 error 是收不了，橙色 retry 是稍后再试，蓝色 info 是已经收录过。"""
    if isinstance(e, projects.AlreadyCollected):
        kind = "info"
    elif isinstance(e, TopicRetryError):
        kind = "retry"
    else:
        kind = "error"
    return {"kind": kind, "text": str(e)}


def _render_submit(request: Request, **context):
    return templates.TemplateResponse(request, "submit.html", context)


@app.get("/")
def index(request: Request, added: int | None = None):
    return templates.TemplateResponse(
        request, "index.html", {"projects": projects.list_all(), "added": added, "active": "home"}
    )


@app.get("/submit")
def submit_page(request: Request):
    return _render_submit(request)


@app.post("/submit")
def submit_preview(request: Request, link: Annotated[str, Form()]):
    """贴链接 → 拉取解析 → 预览。"""
    try:
        topic_id = parse_topic_id(link)
        if projects.exists(topic_id):  # 先查库，收录过的就不用再去请求 linux.do
            raise projects.AlreadyCollected("这个帖子已经收录过了")
        topic = parse_topic(fetch_topic(topic_id))
        projects.check_rules(topic)
    except (TopicError, projects.RuleError) as e:
        return _render_submit(request, link=link, notice=_notice(e))
    _save_preview(topic)
    return _render_submit(request, link=link, topic=topic)


@app.post("/submit/confirm")
def submit_confirm(
    request: Request,
    background_tasks: BackgroundTasks,
    topic_id: Annotated[int, Form()],
    repos: Annotated[list[str], Form()] = [],
):
    """预览里确认 → 收录。"""
    topic = _get_preview(topic_id)
    if topic is None:
        return _render_submit(request, notice={"kind": "retry", "text": "预览已过期，请重新解析"})
    chosen = [r for r in topic.github_repos if r in repos]  # 只认预览时解析出来的仓库，不收表单里多塞的
    if not chosen:
        return _render_submit(request, link=topic.url, topic=topic, confirm_error=True)
    try:
        projects.add(topic, chosen)
    except projects.RuleError as e:
        return _render_submit(request, link=topic.url, notice=_notice(e))
    _previews.pop(topic_id, None)
    # 封面图和头像在后台慢慢下，不让用户等；下好之前首页显示仓库名和首字母
    background_tasks.add_task(projects.save_images, topic.id, chosen[0], topic.author_avatar)
    # 提交成功跳回首页，刷新页面不会重复提交
    return RedirectResponse(f"/?added={topic.id}", status_code=303)
