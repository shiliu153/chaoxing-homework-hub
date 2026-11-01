"""命令行入口。

用法：
    python -m cxreminder run      -c config.yaml     # 守护式轮询
    python -m cxreminder once     -c config.yaml     # 只跑一轮并打印汇总
    python -m cxreminder doctor   -c config.yaml     # 依赖/配置/登录自检
    python -m cxreminder dump     -c config.yaml --out dump/   # 落盘原始响应
    python -m cxreminder test-notify -c config.yaml  # 测试提醒通道
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .utils import ensure_dir, now_in


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level = getattr(logging, "DEBUG" if verbose else cfg.log_level, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if cfg.log_file:
        ensure_dir(os.path.dirname(os.path.abspath(cfg.log_file)))
        file_handler = RotatingFileHandler(
            cfg.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from .runner import Runner

    runner = Runner(cfg)
    try:
        runner.run_forever(max_cycles=args.cycles or cfg.poll.max_cycles)
    except KeyboardInterrupt:
        print("\n已手动停止轮询。")
    return 0


def cmd_once(cfg: Config, args: argparse.Namespace) -> int:
    from .runner import Runner

    runner = Runner(cfg)
    summary = runner.run_once()
    print(summary.to_console())
    counts = summary.counts()
    if counts["errors"] and counts["all"] == 0:
        return 2
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from .client import ChaoxingClient

    problems: List[str] = []
    print(f"cxreminder v{__version__}")
    print(f"Python: {sys.version.split()[0]}")

    for module, hint in (
        ("requests", "pip install requests"),
        ("yaml", "pip install pyyaml"),
        ("Crypto", "pip install pycryptodome"),
    ):
        try:
            __import__(module)
            print(f"[OK] 依赖 {module}")
        except ImportError:
            problems.append(f"缺少依赖 {module}（{hint}）")
            print(f"[!!] 缺少依赖 {module}")

    print(f"[OK] 配置文件：{args.config}")
    print(f"     数据目录：{cfg.storage.data_dir}")
    print(f"     轮询间隔：{cfg.poll.interval_seconds}s，阈值 {cfg.remind.thresholds_hours}h")
    print(f"     提醒通道：{[c.get('type') for c in cfg.channels]}")

    if cfg.account.cookie:
        print("[OK] 使用 Cookie 登录")
    elif cfg.account.username and cfg.account.password:
        masked = cfg.account.username[:3] + "***" + cfg.account.username[-2:]
        print(f"[OK] 账号：{masked}")
    else:
        problems.append("未配置账号或 Cookie")
        print("[!!] 未配置账号")

    if not problems:
        try:
            client = ChaoxingClient(cfg)
            client.login()
            courses = client.fetch_courses()
            print(f"[OK] 登录成功，课程 {len(courses)} 门")
            total = 0
            for course in courses[:3]:
                tasks = client.fetch_course_tasks(course)
                total += len(tasks)
                print(f"     - {course} 拉取 {len(tasks)} 条待办")
            if len(courses) > 3:
                print(f"     - 其余 {len(courses) - 3} 门课程未展开")
            if total == 0:
                print("[??] 未解析到任何待办，可用 dump 子命令保存原始页面后检查解析规则")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"登录/抓取自检失败：{exc}")
            print(f"[!!] 登录/抓取自检失败：{exc}")

    if problems:
        print("\n发现以下问题：")
        for item in problems:
            print(f" - {item}")
        return 1
    print("\n自检通过。")
    return 0


def cmd_dump(cfg: Config, args: argparse.Namespace) -> int:
    from .client import ChaoxingClient

    out_dir = args.out or os.path.join(cfg.storage.data_dir, "dump")
    ensure_dir(out_dir)
    files = ChaoxingClient(cfg).dump(out_dir)
    print(f"已保存 {len(files)} 个文件到 {out_dir}")
    for path in files:
        print(f" - {path}")
    return 0


def cmd_test_notify(cfg: Config, args: argparse.Namespace) -> int:
    from .notify import Notifier

    notifier = Notifier(cfg.channels, dry_run=cfg.dry_run, timeout=cfg.request_timeout)
    title = "学习通提醒：通道测试"
    body = (
        f"### 这是一条测试消息\n\n"
        f"- 发送时间：{now_in(cfg.timezone):%Y-%m-%d %H:%M:%S}\n"
        f"- 若你收到它，说明该通道配置正确。\n"
    )
    results = notifier.send(title, body)
    failed = 0
    for name, ok, detail in results:
        print(f"[{'OK' if ok else '!!'}] {name}: {detail}")
        failed += 0 if ok else 1
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cxreminder",
        description="学习通（超星）未完成作业 / 待参加考试 定时提醒",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"cxreminder {__version__}")

    # 子命令也要能吃 -c/-v（允许 `cxreminder run -c x.yaml`），
    # 用 SUPPRESS 作默认值，避免子解析器把主命令已解析的值覆盖成默认值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="配置文件路径")
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help="输出调试日志")

    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", parents=[common], help="守护式定时轮询（默认）")
    p_run.add_argument("--cycles", type=int, default=0, help="最多轮询几轮后退出，0 为不限")
    sub.add_parser("once", parents=[common], help="只执行一轮并打印汇总")
    sub.add_parser("doctor", parents=[common], help="环境与登录自检")
    p_dump = sub.add_parser("dump", parents=[common], help="保存原始响应便于排查解析")
    p_dump.add_argument("--out", default="", help="输出目录，默认 data/dump")
    sub.add_parser("test-notify", parents=[common], help="向已配置通道发送测试消息")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        args.command = "run"

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    setup_logging(cfg, verbose=args.verbose)

    handlers = {
        "run": cmd_run,
        "once": cmd_once,
        "doctor": cmd_doctor,
        "dump": cmd_dump,
        "test-notify": cmd_test_notify,
    }
    try:
        return handlers[args.command](cfg, args)
    except ConfigError as exc:
        logging.error("配置错误：%s", exc)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
