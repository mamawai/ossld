"""管理员命令行：在服务器上直接收录或删除项目，不走网页提交流程（以后也不校验作者本人）。

python -m app.admin add <帖子链接>...     收录，首楼里的 GitHub 仓库全部收进来
python -m app.admin remove <帖子编号>...  删除
python -m app.admin images               把缺封面图或头像的项目补下载一遍
"""

import argparse
import time

from . import projects
from .linuxdo import TopicError, get_topic


def add(links: list[str]) -> None:
    for i, link in enumerate(links):
        if i:
            time.sleep(16)  # 让开 linux.do 的限流间隔
        try:
            topic = get_topic(link)
            projects.check_rules(topic)
            projects.add(topic, topic.github_repos)
        except (TopicError, projects.RuleError) as e:
            print(f"失败 {link}：{e}")
            continue
        cover = "封面 ✓" if projects.save_cover(topic.id, topic.github_repos[0]) else "封面 ✗"
        avatar = "头像 ✓" if projects.save_avatar(topic.id, topic.author_avatar) else "头像 ✗"
        print(f"已收录 {topic.id} {topic.title}  {' '.join(topic.github_repos)}  {cover} {avatar}")


def remove(ids: list[int]) -> None:
    for topic_id in ids:
        print(f"{'已删除' if projects.remove(topic_id) else '没收录过'} {topic_id}")


def images() -> None:
    missing = [p for p in projects.list_all() if p.cover_url is None or p.avatar_local_url is None]
    for i, p in enumerate(missing):
        if i:
            time.sleep(5)  # GitHub 生成封面图有限流，linux.do 也是，别连着打
        result = []
        if p.cover_url is None:
            result.append("封面 ✓" if projects.save_cover(p.topic_id, p.github_repos[0]) else "封面 ✗")
        if p.avatar_local_url is None:
            result.append("头像 ✓" if projects.save_avatar(p.topic_id, p.author_avatar) else "头像 ✗")
        print(p.topic_id, " ".join(result))
    print(f"共 {len(missing)} 个项目缺图")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description="管理员命令行")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="收录帖子，首楼里的 GitHub 仓库全部收进来")
    p_add.add_argument("links", nargs="+", metavar="帖子链接")
    p_remove = sub.add_parser("remove", help="删除已收录的帖子")
    p_remove.add_argument("ids", nargs="+", type=int, metavar="帖子编号")
    sub.add_parser("images", help="把缺封面图或头像的项目补下载一遍")
    args = parser.parse_args()

    projects.init_db()
    if args.cmd == "add":
        add(args.links)
    elif args.cmd == "remove":
        remove(args.ids)
    else:
        images()


if __name__ == "__main__":
    main()
