"""收录的项目：收录规则、数据库存取、仓库封面图。"""

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from .linuxdo import Topic, avatar_url, topic_url, user_url

REQUIRED_TAG = "开源推广"

DB_PATH = Path(os.environ.get("DATABASE_PATH", Path(__file__).resolve().parent.parent / "data" / "app.db"))
# 封面图和头像都在收录时下载到本地，访客从我们这里加载，不直连 GitHub 和 linux.do
COVERS_DIR = DB_PATH.parent / "covers"
AVATARS_DIR = DB_PATH.parent / "avatars"
_IMAGE_EXTS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

# 标题开头的【开源推广】【开源】[开源自荐] 这类前缀；不带"开源"的方括号可能是项目名，比如 [MetaGateway]，保留
_TITLE_PREFIX = re.compile(r"^(?:\s*[【\[][^】\]]*开源[^】\]]*[】\]])+\s*")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    topic_id      INTEGER PRIMARY KEY,
    title         TEXT    NOT NULL,  -- 原帖标题，原样存，展示时再去前缀
    author_id     INTEGER NOT NULL,
    author        TEXT    NOT NULL,
    author_avatar TEXT    NOT NULL,  -- 头像地址模板
    created_at    TEXT    NOT NULL,  -- 原帖发帖时间，UTC，ISO 格式
    github_repos  TEXT    NOT NULL,  -- JSON 数组
    added_at      TEXT    NOT NULL   -- 收录时间，UTC，ISO 格式
)
"""


class RuleError(Exception):
    """不符合收录规则；异常信息直接展示给用户。"""


class AlreadyCollected(RuleError):
    """帖子已经收录过。"""


@dataclass
class Project:
    topic_id: int
    title: str
    author: str
    author_avatar: str
    created_at: datetime
    github_repos: list[str]

    @property
    def url(self) -> str:
        return topic_url(self.topic_id)

    @property
    def author_url(self) -> str:
        return user_url(self.author)

    @property
    def cover_url(self) -> str | None:
        """封面图下载好了就返回地址；还没有就返回 None，页面上显示仓库名代替。"""
        return f"/covers/{self.topic_id}.png" if cover_path(self.topic_id).exists() else None

    @property
    def avatar_local_url(self) -> str | None:
        """头像下载好了就返回地址；还没有就返回 None，页面上显示用户名首字母。"""
        path = avatar_path(self.topic_id)
        return f"/avatars/{path.name}" if path else None


def display_title(title: str) -> str:
    """展示用的标题：去掉开头的开源类前缀，合并多余空格。"""
    shown = re.sub(r"\s+", " ", _TITLE_PREFIX.sub("", title)).strip()
    return shown or title


def check_rules(topic: Topic) -> None:
    if REQUIRED_TAG not in topic.tags:
        raise RuleError(f"帖子没有「{REQUIRED_TAG}」标签，暂不收录")
    if not topic.github_repos:
        raise RuleError("首楼里没有找到 GitHub 仓库链接，暂不收录")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # 正常结束自动提交，出异常自动回滚
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.execute(_SCHEMA)


def exists(topic_id: int) -> bool:
    with _db() as conn:
        return conn.execute("SELECT 1 FROM projects WHERE topic_id = ?", (topic_id,)).fetchone() is not None


def add(topic: Topic, repos: list[str]) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO projects (topic_id, title, author_id, author, author_avatar, created_at, github_repos, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    topic.id,
                    topic.title,
                    topic.author_id,
                    topic.author,
                    topic.author_avatar,
                    topic.created_at.isoformat(),
                    json.dumps(repos),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except sqlite3.IntegrityError:
        # 两个人同时提交同一个帖子时会走到这里
        raise AlreadyCollected("这个帖子已经收录过了") from None


def remove(topic_id: int) -> bool:
    cover_path(topic_id).unlink(missing_ok=True)
    if path := avatar_path(topic_id):
        path.unlink()
    with _db() as conn:
        return conn.execute("DELETE FROM projects WHERE topic_id = ?", (topic_id,)).rowcount > 0


def list_all() -> list[Project]:
    """按收录时间从新到旧。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT topic_id, title, author, author_avatar, created_at, github_repos FROM projects ORDER BY added_at DESC"
        ).fetchall()
    return [
        Project(
            topic_id=r["topic_id"],
            title=r["title"],
            author=r["author"],
            author_avatar=r["author_avatar"],
            created_at=datetime.fromisoformat(r["created_at"]),
            github_repos=json.loads(r["github_repos"]),
        )
        for r in rows
    ]


def cover_path(topic_id: int) -> Path:
    return COVERS_DIR / f"{topic_id}.png"  # GitHub 的分享图都是 PNG


def avatar_path(topic_id: int) -> Path | None:
    """头像的格式由 linux.do 决定（webp/jpg/png），按扩展名挨个找。"""
    for ext in _IMAGE_EXTS.values():
        path = AVATARS_DIR / f"{topic_id}{ext}"
        if path.exists():
            return path
    return None


def _fetch_image(url: str, attempts: int) -> tuple[bytes, str] | None:
    """下载一张图，429 了等一会儿再试。拿不到返回 None。"""
    for attempt in range(attempts):
        if attempt:
            time.sleep(20)
        try:
            r = requests.get(url, impersonate="chrome", timeout=30)
        except RequestException:
            continue
        content_type = r.headers.get("content-type", "").split(";")[0].strip()
        if r.status_code == 200 and content_type in _IMAGE_EXTS:
            return r.content, content_type
        if r.status_code != 429:
            break
    return None


def _write_file(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")  # 先写临时文件再改名，页面不会读到写了一半的图
    tmp.write_bytes(data)
    tmp.replace(path)


def save_cover(topic_id: int, repo_url: str) -> bool:
    """下载 GitHub 给仓库自动生成的分享图，当作项目封面。"""
    # 中间那段是缓存键，随便填；GitHub 第一次生成某个仓库的图时有限流，多给几次机会
    got = _fetch_image(f"https://opengraph.githubassets.com/1/{repo_url.removeprefix('https://github.com/')}", attempts=3)
    if got is None:
        return False
    _write_file(cover_path(topic_id), got[0])
    return True


def save_avatar(topic_id: int, template: str) -> bool:
    """下载作者在 linux.do 的头像。下 96px 的，页面上 24～32px 显示，高清屏也清楚。"""
    got = _fetch_image(avatar_url(template, 96), attempts=2)
    if got is None:
        return False
    data, content_type = got
    if old := avatar_path(topic_id):  # 作者换了头像格式时，旧文件别留着
        old.unlink()
    _write_file(AVATARS_DIR / f"{topic_id}{_IMAGE_EXTS[content_type]}", data)
    return True


def save_images(topic_id: int, repo_url: str, avatar_template: str) -> None:
    """收录后在后台跑：下载封面图和作者头像。失败了页面有替代显示，管理员可以用命令补。"""
    save_cover(topic_id, repo_url)
    save_avatar(topic_id, avatar_template)
