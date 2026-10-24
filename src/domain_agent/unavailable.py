# -*- coding: utf-8 -*-
"""不可用域名库：持久化存储已确认不可用的域名，供生成阶段参考。

判定为不可用的条件（满足任一即入库）：
  1. 可注册性 = 已注册
  2. 合规风险 = 中 或 高
  3. 历史使用痕迹 = 曾被使用过（ever_used=True）

存储策略：
  - 优先使用 PostgreSQL（通过 DATABASE_URL 环境变量配置，Render 部署用）
  - 未配置 DATABASE_URL 时 fallback 到本地 JSON 文件（本地开发用）
"""

import json
import os
import threading
from datetime import datetime, timezone

# PostgreSQL 连接（懒加载）
_pg_pool = None
_pg_lock = threading.Lock()

# JSON 文件 fallback 路径
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_STORAGE_PATH = os.path.join(_CONFIG_DIR, "unavailable_domains.json")

# 线程锁（JSON 模式用）
_json_lock = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============ PostgreSQL 模式 ============

def _get_pool():
    """获取 PostgreSQL 连接池（懒加载，线程安全）。"""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_lock:
        if _pg_pool is not None:
            return _pg_pool
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return None
        try:
            import psycopg2
            from psycopg2 import pool
        except ImportError:
            print("[unavailable] psycopg2 未安装，回退到 JSON 存储")
            return None
        try:
            _pg_pool = pool.SimpleConnectionPool(1, 10, db_url)
            print("[unavailable] 已连接 PostgreSQL，使用数据库存储不可用域名库")
        except Exception as e:
            print(f"[unavailable] PostgreSQL 连接失败，回退到 JSON 存储：{e}")
            _pg_pool = None
            return None
    return _pg_pool


def init_db():
    """启动时建表（仅 PostgreSQL 模式有效）。"""
    pool = _get_pool()
    if pool is None:
        return
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS unavailable_domains (
                    domain VARCHAR(255) PRIMARY KEY,
                    reason VARCHAR(255) NOT NULL,
                    added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
        conn.commit()
        print("[unavailable] 表 unavailable_domains 已就绪")
    except Exception as e:
        print(f"[unavailable] 建表失败：{e}")
    finally:
        pool.putconn(conn)


def _use_pg():
    return _get_pool() is not None


def _pg_load_domains():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT domain, reason, added_at FROM unavailable_domains ORDER BY added_at DESC"
            )
            rows = cur.fetchall()
        return [
            {
                "domain": r[0],
                "reason": r[1],
                "added_at": r[2].strftime("%Y-%m-%dT%H:%M:%SZ") if r[2] else "",
            }
            for r in rows
        ]
    finally:
        pool.putconn(conn)


def _pg_add_domains(items):
    pool = _get_pool()
    conn = pool.getconn()
    added = 0
    try:
        with conn.cursor() as cur:
            for item in items:
                domain = item.get("domain", "").strip().lower()
                if not domain:
                    continue
                reason = item.get("reason", "")
                cur.execute(
                    """
                    INSERT INTO unavailable_domains (domain, reason, added_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (domain) DO UPDATE
                    SET reason = EXCLUDED.reason, added_at = NOW()
                    """,
                    (domain, reason),
                )
                if cur.rowcount > 0:
                    added += 1
        conn.commit()
    finally:
        pool.putconn(conn)
    return added


def _pg_remove_domain(domain):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM unavailable_domains WHERE domain = %s", (domain,))
            deleted = cur.rowcount
        conn.commit()
        return deleted > 0
    finally:
        pool.putconn(conn)


# ============ JSON fallback 模式 ============

def _json_load_domains():
    if not os.path.exists(JSON_STORAGE_PATH):
        return []
    try:
        with open(JSON_STORAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _json_save_domains(domains):
    with _json_lock:
        with open(JSON_STORAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(domains, f, ensure_ascii=False, indent=2)


def _json_add_domains(items):
    existing = _json_load_domains()
    existing_map = {d["domain"]: d for d in existing}
    added = 0
    now = _now_iso()
    for item in items:
        domain = item.get("domain", "").strip().lower()
        if not domain:
            continue
        reason = item.get("reason", "")
        if domain in existing_map:
            existing_map[domain]["reason"] = reason
            existing_map[domain]["added_at"] = now
        else:
            existing_map[domain] = {"domain": domain, "reason": reason, "added_at": now}
            added += 1
    _json_save_domains(list(existing_map.values()))
    return added


def _json_remove_domain(domain):
    domain = domain.strip().lower()
    existing = _json_load_domains()
    filtered = [d for d in existing if d.get("domain") != domain]
    if len(filtered) < len(existing):
        _json_save_domains(filtered)
        return True
    return False


# ============ 对外统一接口 ============

def load_domains():
    """获取不可用域名列表。"""
    if _use_pg():
        return _pg_load_domains()
    return _json_load_domains()


def add_domains(items):
    """批量添加不可用域名（去重，已存在则更新 reason 与 added_at）。"""
    if _use_pg():
        return _pg_add_domains(items)
    return _json_add_domains(items)


def remove_domain(domain):
    """移除单个域名。"""
    domain = domain.strip().lower()
    if _use_pg():
        return _pg_remove_domain(domain)
    return _json_remove_domain(domain)


def get_domain_set():
    """仅返回域名集合，用于生成时快速查重。"""
    return {d["domain"] for d in load_domains()}
