"""linux.do 帖子的拉取和解析。

只用游客身份请求帖子的 JSON 接口，不带任何登录信息。
带等级的帖子游客看不到，接口返回 404，和帖子不存在分不出来，统一当作不支持。
"""

import json
import math
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

BASE = "https://linux.do"

# 帖子路径：/t/<slug>/<编号>，后面可能跟楼层号。中文标题的 slug 都是 "topic"
_TOPIC_PATH = re.compile(r"/t/[^/]+/(\d+)(?:/\d+)?/?")

# GitHub 的 owner、repo 允许的字符
_GITHUB_NAME = re.compile(r"[A-Za-z0-9._-]+")

# github.com 下这些第一级路径是 GitHub 自己的页面，不是仓库
_GITHUB_RESERVED = {"orgs", "sponsors", "user-attachments", "topics", "features", "marketplace", "settings", "apps"}

# linux.do 限流很严：实测不管间隔 4 秒还是 9 秒，都是第 6 个请求被 429，像是一分钟最多 5 个。
# 所有拉取共用一个最小间隔，被 429 了就冷静一分钟，宁可让用户等，也别把服务器 IP 越封越久
_MIN_INTERVAL = 15.0
_COOLDOWN = 60.0
_next_allowed = 0.0
_request_lock = threading.Lock()


def topic_url(topic_id: int) -> str:
    return f"{BASE}/t/topic/{topic_id}"


def user_url(username: str) -> str:
    return f"{BASE}/u/{quote(username)}"


def avatar_url(template: str, size: int) -> str:
    """头像地址模板 → 实际地址。上传过头像的是站内相对路径，没上传的字母头像可能是完整地址。"""
    url = template.replace("{size}", str(size))
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


class TopicError(Exception):
    """拉取或解析失败；异常信息直接展示给用户。"""


class TopicRetryError(TopicError):
    """临时失败（限流、网络不通），过一会儿再试就行。"""


@dataclass
class Topic:
    id: int
    url: str
    title: str
    author_id: int  # 论坛用户 id；用户名能改，认人用这个
    author: str
    author_url: str
    author_avatar: str  # 头像地址模板，{size} 换成像素数
    created_at: datetime
    tags: list[str]
    github_repos: list[str]  # 规整成 https://github.com/owner/repo，已去重


def parse_topic_id(link: str) -> int:
    """从帖子链接里取出帖子编号。"""
    u = urlparse(link.strip())
    m = _TOPIC_PATH.fullmatch(u.path)
    if u.scheme not in ("http", "https") or u.hostname != "linux.do" or not m:
        raise TopicError("不是 linux.do 的帖子链接，应该像 https://linux.do/t/topic/123456")
    return int(m.group(1))


def _take_request_slot() -> None:
    """还没到下一次允许请求的时间就直接报错，不在这里干等。"""
    global _next_allowed
    with _request_lock:
        wait = _next_allowed - time.monotonic()
        if wait > 0:
            raise TopicRetryError(f"提交的人有点多，请 {math.ceil(wait)} 秒后再试")
        _next_allowed = time.monotonic() + _MIN_INTERVAL


def _cool_down() -> None:
    global _next_allowed
    with _request_lock:
        _next_allowed = max(_next_allowed, time.monotonic() + _COOLDOWN)


def fetch_topic(topic_id: int) -> dict:
    """匿名拉取帖子 JSON。"""
    _take_request_slot()
    # 地址自己拼，不直接请求用户给的链接，防止服务器被当跳板
    url = f"{topic_url(topic_id)}.json"
    try:
        # 要模拟 Chrome 的 TLS 指纹，普通 HTTP 客户端会被 Cloudflare 直接 403
        r = requests.get(url, impersonate="chrome", timeout=15)
    except RequestException as e:
        raise TopicRetryError("连不上 linux.do，请稍后再试") from e
    if r.status_code == 200:
        return r.json()
    # 论坛自己的拒绝是 JSON；Cloudflare 的拦截、限流页是 HTML
    if r.status_code in (403, 404) and "json" in r.headers.get("content-type", ""):
        raise TopicError("帖子不存在，或需要登录、达到等级才能查看，暂不支持")
    if r.status_code == 429:
        _cool_down()
    raise TopicRetryError(f"linux.do 暂时拒绝了请求（HTTP {r.status_code}），请稍后再试")


def parse_topic(data: dict) -> Topic:
    """从帖子 JSON 里取出需要的字段。"""
    op = data["post_stream"]["posts"][0]  # 不带楼层号请求时，第一条就是首楼
    return Topic(
        id=data["id"],
        url=topic_url(data["id"]),
        title=data["title"],
        author_id=op["user_id"],
        author=op["username"],
        author_url=user_url(op["username"]),
        author_avatar=op["avatar_template"],
        created_at=datetime.fromisoformat(data["created_at"]),
        tags=[t["name"] for t in data["tags"]],
        github_repos=github_repos(op["cooked"]),
    )


class _LinkCollector(HTMLParser):
    """收集 HTML 里所有 <a href>。"""

    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def github_repos(cooked: str) -> list[str]:
    """从首楼 HTML 里挑出 GitHub 仓库链接。

    链接后面的 /tree/...、#锚点、.git 都去掉，只留 owner/repo；
    xxx.github.io 是网站不是仓库，不算。
    """
    collector = _LinkCollector()
    collector.feed(cooked)
    repos, seen = [], set()
    for href in collector.hrefs:
        u = urlparse(href)
        if u.hostname not in ("github.com", "www.github.com"):
            continue
        parts = [s for s in u.path.split("/") if s]
        if len(parts) < 2 or parts[0].lower() in _GITHUB_RESERVED:
            continue
        owner, repo = parts[0], parts[1].removesuffix(".git")
        if not (_GITHUB_NAME.fullmatch(owner) and _GITHUB_NAME.fullmatch(repo)):
            continue
        key = f"{owner}/{repo}".lower()  # GitHub 仓库名不分大小写
        if key not in seen:
            seen.add(key)
            repos.append(f"https://github.com/{owner}/{repo}")
    return repos


def get_topic(link: str) -> Topic:
    """帖子链接 → 解析好的帖子信息。"""
    return parse_topic(fetch_topic(parse_topic_id(link)))


if __name__ == "__main__":
    # 命令行试用：python -m app.linuxdo <帖子链接>
    if len(sys.argv) != 2:
        sys.exit("用法：python -m app.linuxdo <帖子链接>")
    try:
        topic = get_topic(sys.argv[1])
    except TopicError as e:
        sys.exit(f"失败：{e}")
    print(json.dumps(asdict(topic), ensure_ascii=False, indent=2, default=str))
