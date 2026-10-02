#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
biliup 自动清理脚本

功能：
1) 扫描 /mnt/data/biliup/backup/ 目录，把 mtime 超过 48 小时的文件/子目录
   移入 1Panel 回收站（/.1panel_clash/），并按 1Panel 命名规则改名。
2) 扫描 1Panel 回收站 /.1panel_clash/，把“进入回收站时间戳”超过 48 小时的条目
   彻底删除（这是唯一允许永久删除的地方，仅在回收站内操作）。

1Panel 回收站命名规则（实测）：
  _1p_file_ + 原/路径中每个 / 替换为 _1p_ + _p_<原大小字节>_<删除时Unix时间戳>
  示例：/opt/x/cover (408898B, 删除于1784512811)
        -> _1p_file_1p_opt_1p_x_1p_cover_p_408898_1784512811

安全特性：
  - 只会写入/删除两个固定目录，绝不会递归到别处；
  - 永久删除仅发生在 /.1panel_clash 内，源 backup 目录只是「移动」而非删除；
  - 寻找目标：
      备份区  -> mtime（内容最后修改时间）
      回收站  -> 取回收站文件名末尾的删除时间戳（进入回收站时刻）
  - 带 --dry-run：只打印将要做什么，不真正执行；
  - 带 --verbose：打印每个动作详情；
  - 原子动作：移动用 os.rename（跨设备会自动 fallback 到跨设备 move），始终“回收优先”，不做物理删除。
  - 严格按用户全局规则：全程不调用 rm -rf，永久删除用 Python 逐条校验后删除。
"""

import os
import sys
import time
import shutil
import argparse
from pathlib import Path

# ---------- 配置 ----------
BACKUP_DIR = Path("/mnt/data/biliup/backup")
RECYCLE_DIR = Path("/.1panel_clash")
AGE_HOURS = 48
LOG_FILE = Path("/var/log/biliup_clean.log")
# ---------------------------


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 日志写不进去不应阻断清理


def safe_to_recycle_name(path: Path) -> str:
    """按 1Panel 规则生成回收站文件名。
    实测 1Panel 真实命名：_1p_file + 路径中每个 / 替换为 _1p_ + _p_<size>_<ts>
    例：/opt/x/cover -> _1p_file_1p_opt_1p_x_1p_cover_p_<size>_<ts>
    注意：encoded 本身以 _1p_ 开头（由开头的 / 转来），所以前缀用 '_1p_file'
    （不带尾下划线）拼接后正好是单下划线，与 1Panel 一致。
    """
    abs_path = str(path.resolve())
    encoded = abs_path.replace("/", "_1p_")  # 以 _1p_ 开头
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    ts = int(time.time())
    return f"_1p_file{encoded}_p_{size}_{ts}"


def age_seconds_by_mtime(path: Path, now: int) -> float:
    """基于内容 mtime 计算文件/目录的“年龄”秒数。"""
    try:
        return now - int(path.stat().st_mtime)
    except OSError:
        return float("inf")


def move_to_recycle(src: Path, dry_run: bool, verbose: bool) -> bool:
    """把 backup 下的一个文件/目录移入回收站。"""
    recycle_name = safe_to_recycle_name(src)
    dst = RECYCLE_DIR / recycle_name
    if dst.exists():
        # 极小概率重名（同一秒删同名），加后缀避让
        dst = RECYCLE_DIR / f"{recycle_name}.{int(time.time()*1000)%100000}"
    if verbose:
        log(f"RECYCLE: {src}  ->  {dst}")
    if dry_run:
        return True
    try:
        RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.move(str(src), str(dst))
        else:
            shutil.move(str(src), str(dst))
        return True
    except Exception as e:
        log(f"  ! 移入回收站失败 {src}: {e}")
        return False


def parse_recycle_timestamp(name: str):
    """从回收站文件名末尾解析“进入回收站的时间戳”。
    返回 None 表示无法解析（非 1Panel 规范条目，跳过不删，保守处理）。
    """
    # 规则：以 _p_<size>_<timestamp> 结尾，timestamp 为纯数字
    if "_p_" not in name:
        return None
    tail = name.rsplit("_p_", 1)[1]  # "<size>_<timestamp>"
    if "_" not in tail:
        return None
    size_part, _, ts_part = tail.rpartition("_")
    if not ts_part.isdigit():
        return None
    if not size_part.isdigit():
        # size 可能不是纯数字（1Panel 对目录大小写法可能不同）；仅要求 ts 合法即可
        pass
    try:
        return int(ts_part)
    except ValueError:
        return None


def purge_recycle_item(item: Path, dry_run: bool, verbose: bool) -> bool:
    """从回收站里彻底删除一个条目。这是整个脚本里唯一的物理删除点。"""
    if verbose:
        kind = "DIR " if item.is_dir() else "FILE"
        if dry_run:
            kind = "DRY-" + kind
        log(f"PURGE {kind}: {item}")
    if dry_run:
        return True
    try:
        if item.is_dir() and not item.is_symlink():
            # 校验：必须在回收站内，避免误删
            item_resolved = item.resolve()
            recycle_resolved = RECYCLE_DIR.resolve()
            if not str(item_resolved).startswith(str(recycle_resolved) + "/"):
                log(f"  ! 安全检查失败，跳过 {item}")
                return False
            shutil.rmtree(item_resolved)
        else:
            item.unlink()
        return True
    except Exception as e:
        log(f"  ! 彻底删除失败 {item}: {e}")
        return False


def sweep_backup(dry_run: bool, verbose: bool, age_hours: int) -> int:
    """第一段：把备份目录里超过 age_hours 的条目移入回收站。"""
    now = int(time.time())
    moved = 0
    if not BACKUP_DIR.exists():
        log(f"备份目录不存在，跳过: {BACKUP_DIR}")
        return 0
    try:
        entries = list(BACKUP_DIR.iterdir())
    except Exception as e:
        log(f"无法读取备份目录 {BACKUP_DIR}: {e}")
        return 0

    for entry in entries:
        age = age_seconds_by_mtime(entry, now)
        if age >= age_hours * 3600:
            # 双重保险：只处理 BACKUP_DIR 的直接子项
            try:
                if entry.resolve().parent.resolve() != BACKUP_DIR.resolve():
                    continue
            except Exception:
                continue
            if move_to_recycle(entry, dry_run, verbose):
                moved += 1
    return moved


def sweep_recycle(dry_run: bool, verbose: bool, age_hours: int) -> int:
    """第二段：回收站中进入超过 age_hours 的条目彻底删除。"""
    now = int(time.time())
    purged = 0
    if not RECYCLE_DIR.exists():
        log(f"回收站不存在，跳过: {RECYCLE_DIR}")
        return 0
    try:
        entries = list(RECYCLE_DIR.iterdir())
    except Exception as e:
        log(f"无法读取回收站 {RECYCLE_DIR}: {e}")
        return 0

    for entry in entries:
        # 仅处理 1Panel 规范条目；非规范条目保守不动
        ts = parse_recycle_timestamp(entry.name)
        if ts is None:
            if verbose:
                log(f"SKIP (非1Panel命名): {entry.name}")
            continue
        age = now - ts
        if age >= age_hours * 3600:
            if purge_recycle_item(entry, dry_run, verbose):
                purged += 1
    return purged


def main():
    parser = argparse.ArgumentParser(description="biliup backup 清理 + 1Panel 回收站清理")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不真正执行")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每个动作")
    parser.add_argument("--age-hours", type=int, default=AGE_HOURS, help=f"过期阈值小时数，默认 {AGE_HOURS}")
    args = parser.parse_args()

    log("=" * 60)
    log(f"开始清理 | dry-run={args.dry_run} | age={args.age_hours}h")
    log(f"备份目录: {BACKUP_DIR}")
    log(f"回收站:   {RECYCLE_DIR}")

    moved = sweep_backup(args.dry_run, args.verbose, args.age_hours)
    log(f"第一段完成：移入回收站 {moved} 项")

    purged = sweep_recycle(args.dry_run, args.verbose, args.age_hours)
    log(f"第二段完成：彻底删除 {purged} 项")
    log("结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())