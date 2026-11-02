# -*- coding: utf-8 -*-
"""
================================================================================
数据中台 (Data Platform) 全局会话与对话记忆管理模块 (chat_history_db.py)

【模块定位】
负责企业级数据中台中多智能体（Multi-Agent）会话生命周期与消息历史的持久化存储：
1. 统一数据资产：存储所有接入中台的业务 Agent 的对话 Session 与消息流水；
2. 多租户隔离：原生内置 `agent_id` 字段，支持不同业务系统 Agent 独立索引与跨智能体共享；
3. 支持步骤轨迹存储：持久化保存 Agent 的思考过程（thought）与工具调用（steps_json）。
================================================================================
"""

import os
import json
import sqlite3
import uuid
import datetime
from typing import List, Dict, Any, Optional

BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
DB_FILE_PATH: str = os.path.join(BASE_DIR, "chat_history.db")


class DataPlatformChatDB:
    """
    【类名】DataPlatformChatDB
    【功能】封装数据中台对 SQLite 会话与历史消息库的高性能持久化操作
    """

    def __init__(self, db_path: str = DB_FILE_PATH):
        self.db_path = db_path
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. 会话表 (chat_sessions) 支持 agent_id
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    agent_id TEXT DEFAULT 'agentic_rag_core',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_agent ON chat_sessions(agent_id)")

            # 2. 消息表 (chat_messages)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    steps_json TEXT,
                    referenced_docs_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_sess ON chat_messages(session_id)")
            conn.commit()

    def create_session(self, title: str = "新对话", agent_id: str = "agentic_rag_core") -> Dict[str, Any]:
        """为特定 Agent 创建新会话"""
        session_id = str(uuid.uuid4())
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_sessions (session_id, title, agent_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, title, agent_id, now_str, now_str))
            conn.commit()

        return {
            "session_id": session_id,
            "title": title,
            "agent_id": agent_id,
            "created_at": now_str,
            "updated_at": now_str
        }

    def list_sessions(self, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取会话列表，可按 agent_id 过滤，按更新时间倒序"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if agent_id:
                cursor.execute("""
                    SELECT session_id, title, agent_id, created_at, updated_at
                    FROM chat_sessions
                    WHERE agent_id = ?
                    ORDER BY updated_at DESC
                """, (agent_id,))
            else:
                cursor.execute("""
                    SELECT session_id, title, agent_id, created_at, updated_at
                    FROM chat_sessions
                    ORDER BY updated_at DESC
                """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取单个会话的信息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT session_id, title, agent_id, created_at, updated_at
                FROM chat_sessions
                WHERE session_id = ?
            """, (session_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_session_title(self, session_id: str, title: str) -> bool:
        """更新会话标题"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE chat_sessions
                SET title = ?, updated_at = ?
                WHERE session_id = ?
            """, (title, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id))
            conn.commit()
            return cursor.rowcount > 0

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        steps: Optional[List[Dict[str, Any]]] = None,
        referenced_docs: Optional[List[str]] = None
    ) -> int:
        """向指定会话追加消息并刷新会话时间戳"""
        steps_json = json.dumps(steps, ensure_ascii=False) if steps else None
        referenced_docs_json = json.dumps(referenced_docs, ensure_ascii=False) if referenced_docs else None
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_messages (session_id, role, content, steps_json, referenced_docs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, role, content, steps_json, referenced_docs_json, now_str))
            msg_id = cursor.lastrowid

            cursor.execute("""
                UPDATE chat_sessions
                SET updated_at = ?
                WHERE session_id = ?
            """, (now_str, session_id))

            conn.commit()
            return msg_id

    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """获取会话全部历史消息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, session_id, role, content, steps_json, referenced_docs_json, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY id ASC
            """, (session_id,))
            rows = cursor.fetchall()

            messages = []
            for r in rows:
                steps = json.loads(r["steps_json"]) if r["steps_json"] else []
                referenced_docs = json.loads(r["referenced_docs_json"]) if r["referenced_docs_json"] else []
                messages.append({
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "steps": steps,
                    "referenced_docs": referenced_docs,
                    "created_at": r["created_at"]
                })
            return messages

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话及关联消息"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_stats(self) -> Dict[str, int]:
        """获取全局会话与消息统计"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM chat_sessions")
            total_sessions = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM chat_messages")
            total_messages = cursor.fetchone()[0]
            return {
                "total_sessions": total_sessions,
                "total_messages": total_messages
            }


_global_data_chat_db: Optional[DataPlatformChatDB] = None

def get_data_chat_db() -> DataPlatformChatDB:
    global _global_data_chat_db
    if _global_data_chat_db is None:
        _global_data_chat_db = DataPlatformChatDB()
    return _global_data_chat_db
