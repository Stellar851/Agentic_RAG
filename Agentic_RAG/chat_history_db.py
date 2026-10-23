"""
================================================================================
Agentic RAG 企业级智能关联知识库系统
对话历史与多会话持久化存储模块 (chat_history_db.py)

【模块定位】
本文件负责会话（Session）与消息历史（Messages）的持久化管理：
1. 采用 Python 内置的 SQLite 数据库，零额外依赖，无需配置外部服务。
2. 数据表设计：
   - `chat_sessions`: 记录会话元数据（会话ID、会话标题、创建与最近活跃时间）。
   - `chat_messages`: 记录每一轮消息（角色 user/assistant、问答正文、Agent 思考证据链 steps、引用文档）。
3. 支持完整生命周期操作：
   - 创建新会话、会话列表按时间倒序排列
   - 消息追加、读取某会话的完整历史、删除会话、动态更新会话标题
4. 彻底解决网页刷新后历史丢失、多话题无法切换分类的核心痛点。
================================================================================
"""

import os
import json
import sqlite3
import uuid
import datetime
from typing import List, Dict, Any, Optional

# 数据库存储路径 (放在 Agentic_RAG 项目根目录下)
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
DB_FILE_PATH: str = os.path.join(BASE_DIR, "chat_history.db")


class ChatHistoryDB:
    """
    【类名】ChatHistoryDB
    【功能】封装对 SQLite 历史会话数据库的所有增删查改操作
    """

    def __init__(self, db_path: str = DB_FILE_PATH):
        self.db_path = db_path
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        """获取 SQLite 连接，设置 row_factory 为 Row 以支持字典风格访问"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        """初始化数据表结构"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. 会话表 (chat_sessions)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. 消息表 (chat_messages)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,              -- 'user' 或 'assistant'
                    content TEXT NOT NULL,
                    steps_json TEXT,                 -- Agent 思考轨迹 JSON 序列化字符串
                    referenced_docs_json TEXT,       -- 引用来源文档列表 JSON
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id) ON DELETE CASCADE
                );
            """)
            conn.commit()

    def create_session(self, title: str = "新对话") -> Dict[str, Any]:
        """
        【功能】创建一个新的会话
        【返回值】包含新 session_id 与基本信息的字典
        """
        session_id = str(uuid.uuid4())
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_sessions (session_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (session_id, title, now_str, now_str))
            conn.commit()

        return {
            "session_id": session_id,
            "title": title,
            "created_at": now_str,
            "updated_at": now_str
        }

    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        【功能】获取所有会话列表，按最后更新时间倒序排列
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT session_id, title, created_at, updated_at
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
                SELECT session_id, title, created_at, updated_at
                FROM chat_sessions
                WHERE session_id = ?
            """, (session_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_session_title(self, session_id: str, title: str) -> bool:
        """更新会话标题（通常在第一次用户提问后自动生成标题）"""
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
        """
        【功能】向指定会话追加一条消息，并自动刷新会话的 updated_at 时间
        """
        steps_json = json.dumps(steps, ensure_ascii=False) if steps else None
        referenced_docs_json = json.dumps(referenced_docs, ensure_ascii=False) if referenced_docs else None
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. 插入消息
            cursor.execute("""
                INSERT INTO chat_messages (session_id, role, content, steps_json, referenced_docs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, role, content, steps_json, referenced_docs_json, now_str))
            msg_id = cursor.lastrowid

            # 2. 更新对应会话的活跃时间
            cursor.execute("""
                UPDATE chat_sessions
                SET updated_at = ?
                WHERE session_id = ?
            """, (now_str, session_id))

            conn.commit()
            return msg_id

    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """
        【功能】获取某个会话的所有历史消息，按时间正序排列
        """
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
        """
        【功能】删除会话及其所有关联消息
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0


# 全局单例管理器
_global_chat_db: Optional[ChatHistoryDB] = None

def get_chat_db() -> ChatHistoryDB:
    """获取会话历史数据库管理器的单例"""
    global _global_chat_db
    if _global_chat_db is None:
        _global_chat_db = ChatHistoryDB()
    return _global_chat_db
