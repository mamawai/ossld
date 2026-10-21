"""管理员命令行：在服务器上改分类、删除项目、补下载图片。收录只走网页提交。

python -m app.admin category <帖子编号> <分类>  改分类
python -m app.admin remove <帖子编号>...       删除
python -m app.admin images                    把缺封面图的项目补下载一遍
"""

import argparse
import time

from . import projects


def remove(ids: list[int]) -> None:
    for topic_id in ids:
        print(f"{'已删除' if projects.remove(topic_id) else '没收录过'} {topic_id}")


def images() -> None:
    missing = [p for p in projects.list_all() if p.cover_url is None]
    for i, p in enumerate(missing):
        if i:
            time.sleep(5)  # GitHub 生成封面图有限流，别连着打
        print(p.topic_id, "封面 ✓" if projects.save_cover(p.topic_id, p.github_repos[0]) else "封面 ✗")
    print(f"共 {len(missing)} 个项目缺封面图")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description="管理员命令行")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_cat = sub.add_parser("category", help="改分类")
    p_cat.add_argument("topic_id", type=int, metavar="帖子编号")
    p_cat.add_argument("category", choices=projects.CATEGORIES, metavar="分类", help="、".join(projects.CATEGORIES))
    p_remove = sub.add_parser("remove", help="删除已收录的帖子")
    p_remove.add_argument("ids", nargs="+", type=int, metavar="帖子编号")
    sub.add_parser("images", help="把缺封面图的项目补下载一遍")
    args = parser.parse_args()

    projects.init_db()
    if args.cmd == "category":
        ok = projects.set_category(args.topic_id, args.category)
        print(f"{args.topic_id} → {args.category}" if ok else f"没收录过 {args.topic_id}")
    elif args.cmd == "remove":
        remove(args.ids)
    else:
        images()


if __name__ == "__main__":
    main()
