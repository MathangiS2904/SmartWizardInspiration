"""
inspiration_db.py

Database manager for Smart Wizard Inspiration API testing.
Supports MySQL (via DATABASE_URL from .env or environment) with automatic SQLite fallback.
Stores all API errors (400, 404, 500) and test run execution summaries in the database.
"""

import os
import sys
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    Text,
    DateTime,
    select,
    text
)
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DEFAULT_DB_FILE = "inspiration_errors.db"
DEFAULT_MYSQL_DB = "smartwizard_inspiration"
RAW_DATABASE_URL = os.getenv("DATABASE_URL", "")

Base = declarative_base()


class ApiError(Base):
    """Table to store API errors (400, 404, 500, etc.)."""
    __tablename__ = "api_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(String(30), nullable=False)
    room = Column(String(255), nullable=False)
    style_id = Column(Integer, nullable=True)
    status_code = Column(Integer, nullable=False)
    error_message = Column(Text, nullable=True)
    retry_attempt = Column(Integer, default=0)
    input_url = Column(Text, nullable=True)
    input_path = Column(Text, nullable=True)
    blob_url = Column(Text, nullable=True)
    blob_path = Column(Text, nullable=True)
    template_name = Column(String(255), nullable=True)
    template_url = Column(Text, nullable=True)


class ApiResult(Base):
    """Table to store all test execution results (200 OK, FAILED, etc.)."""
    __tablename__ = "api_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(String(30), nullable=False)
    room = Column(String(255), nullable=False)
    room_type = Column(String(100), nullable=True)
    style_id = Column(Integer, nullable=True)
    status = Column(String(50), nullable=False)
    status_code = Column(Integer, nullable=False)
    latency = Column(Float, nullable=True)
    items_count = Column(Integer, default=0)
    retries_used = Column(Integer, default=0)
    saved_to = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)
    input_url = Column(Text, nullable=True)
    input_path = Column(Text, nullable=True)
    blob_url = Column(Text, nullable=True)
    blob_path = Column(Text, nullable=True)
    template_name = Column(String(255), nullable=True)
    template_url = Column(Text, nullable=True)


def resolve_database_url(raw_url: Optional[str] = None) -> str:
    """
    Resolves the database URL:
    - Prioritizes DATABASE_URL from .env if valid (e.g. MySQL).
    - Formats SQLite URLs appropriately if a file path is provided.
    - Automatically creates the database on MySQL server if it doesn't exist yet.
    """
    env_url = (os.getenv("DATABASE_URL") or RAW_DATABASE_URL or "").strip()

    if raw_url and (raw_url.startswith("mysql") or raw_url.startswith("sqlite://")):
        candidate = raw_url
    elif env_url:
        candidate = env_url
    elif raw_url and raw_url != DEFAULT_DB_FILE:
        candidate = f"sqlite:///{raw_url}"
    else:
        candidate = f"sqlite:///{DEFAULT_DB_FILE}"

    url = candidate.strip()

    if url.startswith("mysql"):
        parsed = urlparse(url)
        db_name = parsed.path.lstrip("/")
        if not db_name:
            db_name = DEFAULT_MYSQL_DB
            url = url.rstrip("/") + "/" + db_name

        # Pre-verify/create database on MySQL server
        try:
            import pymysql
            conn_temp = pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=parsed.username,
                password=parsed.password
            )
            with conn_temp.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}`;")
            conn_temp.close()
        except Exception as e:
            print(f"[!] Warning: Could not pre-verify MySQL DB creation: {e}", file=sys.stderr)

    elif not url.startswith("sqlite://"):
        url = f"sqlite:///{url}"

    return url


class DatabaseManager:
    """Handles engine, session, and CRUD operations for both MySQL and SQLite."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = resolve_database_url(db_url)
        self.is_mysql = self.db_url.startswith("mysql")
        try:
            self.engine = create_engine(self.db_url, pool_pre_ping=True)
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)
            self.available = True
            # Ensure new columns exist on existing MySQL/SQLite tables
            try:
                with self.engine.connect() as conn:
                    for tbl in ["api_results", "api_errors"]:
                        cols_to_add = [
                            "input_url", "input_path", "blob_url", "blob_path", 
                            "template_name", "template_url"
                        ]
                        if tbl == "api_results":
                            cols_to_add.append("reason")
                        for col in cols_to_add:
                            try:
                                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT;"))
                                conn.commit()
                            except Exception:
                                pass
            except Exception:
                pass
        except Exception as e:
            print(f"[!] Database connection error with {self.db_url}: {e}", file=sys.stderr)
            print(f"[*] Falling back to local SQLite ({DEFAULT_DB_FILE})...", file=sys.stderr)
            self.db_url = f"sqlite:///{DEFAULT_DB_FILE}"
            self.is_mysql = False
            self.engine = create_engine(self.db_url)
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)
            self.available = True

    def get_session(self):
        return self.SessionLocal()

    def log_api_error(
        self,
        room: str,
        style_id: Optional[int],
        status_code: int,
        error_message: str,
        retry_attempt: int = 0,
        input_url: Optional[str] = None,
        input_path: Optional[str] = None,
        template_name: Optional[str] = None
    ) -> int:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        is_azure = bool(input_url and "blob.core.windows.net" in str(input_url))
        final_input_url = str(input_url) if (input_url and is_azure) else None
        final_input_path = str(input_path) if (input_path and is_azure) else None
        final_template_name = str(template_name) if template_name else (os.path.basename(input_path) if input_path else None)

        with self.get_session() as session:
            err = ApiError(
                timestamp=now_str,
                room=str(room),
                style_id=style_id,
                status_code=status_code,
                error_message=str(error_message).strip(),
                retry_attempt=retry_attempt,
                input_url=final_input_url,
                input_path=final_input_path,
                blob_url=final_input_url,
                blob_path=final_input_path,
                template_name=final_template_name,
                template_url=final_input_url
            )
            session.add(err)
            session.commit()
            session.refresh(err)
            return err.id

    def log_api_result(
        self,
        room: str,
        room_type: str,
        style_id: Optional[int],
        status: str,
        status_code: int,
        latency: float,
        items_count: int,
        retries_used: int = 0,
        saved_to: Optional[str] = None,
        reason: Optional[str] = None,
        input_url: Optional[str] = None,
        input_path: Optional[str] = None,
        template_name: Optional[str] = None
    ) -> int:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not reason:
            if status_code == 200:
                reason = f"Successfully placed {items_count} items (Style #{style_id})"
                if retries_used > 0:
                    reason += f" after {retries_used} retries"
            else:
                reason = f"Completed with status {status_code}"

        is_azure = bool(input_url and "blob.core.windows.net" in str(input_url))
        final_input_url = str(input_url) if (input_url and is_azure) else None
        final_input_path = str(input_path) if (input_path and is_azure) else None
        final_template_name = str(template_name) if template_name else (os.path.basename(input_path) if input_path else None)

        with self.get_session() as session:
            res = ApiResult(
                timestamp=now_str,
                room=str(room),
                room_type=str(room_type),
                style_id=style_id,
                status=str(status),
                status_code=status_code,
                latency=round(latency, 2),
                items_count=items_count,
                retries_used=retries_used,
                saved_to=saved_to,
                reason=str(reason),
                input_url=final_input_url,
                input_path=final_input_path,
                blob_url=final_input_url,
                blob_path=final_input_path,
                template_name=final_template_name,
                template_url=final_input_url
            )
            session.add(res)
            session.commit()
            session.refresh(res)
            return res.id

    def get_all_errors(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_session() as session:
            stmt = select(ApiError).order_by(ApiError.id.desc()).limit(limit)
            results = session.execute(stmt).scalars().all()
            return [
                {
                    "id": r.id,
                    "timestamp": r.timestamp,
                    "room": r.room,
                    "style_id": r.style_id,
                    "status_code": r.status_code,
                    "error_message": r.error_message,
                    "retry_attempt": r.retry_attempt,
                    "reason": r.error_message,  # Alias for consistent frontend UI
                    "input_url": getattr(r, "input_url", None) or getattr(r, "template_url", None) or getattr(r, "blob_url", None),
                    "input_path": getattr(r, "input_path", None) or getattr(r, "blob_path", None)
                }
                for r in results
            ]

    def get_all_results(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_session() as session:
            stmt = select(ApiResult).order_by(ApiResult.id.desc()).limit(limit)
            results = session.execute(stmt).scalars().all()
            return [
                {
                    "id": r.id,
                    "timestamp": r.timestamp,
                    "room": r.room,
                    "room_type": r.room_type,
                    "style_id": r.style_id,
                    "status": r.status,
                    "status_code": r.status_code,
                    "latency": r.latency,
                    "items_count": r.items_count,
                    "retries_used": r.retries_used,
                    "saved_to": r.saved_to,
                    "reason": r.reason or (f"Placed {r.items_count} items (Style #{r.style_id})" if r.status_code == 200 else f"HTTP {r.status_code}"),
                    "input_url": getattr(r, "input_url", None) or getattr(r, "template_url", None) or getattr(r, "blob_url", None),
                    "input_path": getattr(r, "input_path", None) or getattr(r, "blob_path", None)
                }
                for r in results
            ]


# Singleton instance
_db_manager = None


def get_db(db_url: Optional[str] = None) -> DatabaseManager:
    global _db_manager
    if _db_manager is None or db_url is not None:
        _db_manager = DatabaseManager(db_url=db_url)
    return _db_manager


def init_db(db_url: Optional[str] = None) -> None:
    """Initializes the database connection and tables."""
    get_db(db_url)


def log_api_error(
    room: str,
    style_id: Optional[int],
    status_code: int,
    error_message: str,
    retry_attempt: int = 0,
    db_path: Optional[str] = None,
    input_url: Optional[str] = None,
    input_path: Optional[str] = None,
    template_name: Optional[str] = None
) -> int:
    return get_db(db_path).log_api_error(
        room=room,
        style_id=style_id,
        status_code=status_code,
        error_message=error_message,
        retry_attempt=retry_attempt,
        input_url=input_url,
        input_path=input_path,
        template_name=template_name
    )


def log_api_result(
    room: str,
    room_type: str,
    style_id: Optional[int],
    status: str,
    status_code: int,
    latency: float,
    items_count: int,
    retries_used: int = 0,
    saved_to: Optional[str] = None,
    db_path: Optional[str] = None,
    reason: Optional[str] = None,
    input_url: Optional[str] = None,
    input_path: Optional[str] = None,
    template_name: Optional[str] = None
) -> int:
    return get_db(db_path).log_api_result(
        room=room,
        room_type=room_type,
        style_id=style_id,
        status=status,
        status_code=status_code,
        latency=latency,
        items_count=items_count,
        retries_used=retries_used,
        saved_to=saved_to,
        reason=reason,
        input_url=input_url,
        input_path=input_path,
        template_name=template_name
    )


def print_db_errors(db_url: Optional[str] = None, limit: int = 50) -> None:
    """Prints a formatted table of all errors stored in MySQL or SQLite."""
    manager = get_db(db_url)
    errors = manager.get_all_errors(limit=limit)
    engine_name = "MySQL" if manager.is_mysql else "SQLite"

    print("\n" + "=" * 110)
    print(f"{'RECORDED API ERRORS (' + engine_name.upper() + ' DATABASE: ' + manager.db_url + ')':^110}")
    print("=" * 110)
    if not errors:
        print("No errors currently recorded in database.")
        print("=" * 110 + "\n")
        return

    print(f"{'ID':<5} {'Timestamp':<20} {'Room':<22} {'Style':<7} {'Status':<8} {'Retry':<6} {'Error Message'}")
    print("-" * 110)
    for err in errors:
        msg = (err['error_message'] or "").replace("\n", " ")
        if len(msg) > 42:
            msg = msg[:39] + "..."
        print(
            f"{err['id']:<5} {err['timestamp']:<20} {err['room']:<22} "
            f"{str(err['style_id']):<7} {err['status_code']:<8} "
            f"{err['retry_attempt']:<6} {msg}"
        )
    print("=" * 110 + "\n")


def print_db_results(db_url: Optional[str] = None, limit: int = 50) -> None:
    """Prints a formatted table of all test run results stored in MySQL or SQLite."""
    manager = get_db(db_url)
    results = manager.get_all_results(limit=limit)
    engine_name = "MySQL" if manager.is_mysql else "SQLite"

    print("\n" + "=" * 110)
    print(f"{'RECORDED API RESULTS (' + engine_name.upper() + ' DATABASE)':^110}")
    print("=" * 110)
    if not results:
        print("No test runs currently recorded.")
        print("=" * 110 + "\n")
        return

    print(f"{'ID':<5} {'Timestamp':<20} {'Room':<20} {'Type':<12} {'Style':<6} {'Status':<10} {'Latency':<8} {'Items':<7}")
    print("-" * 110)
    for r in results:
        lat = f"{r['latency']:.2f}s" if r['latency'] is not None else "-"
        print(
            f"{r['id']:<5} {r['timestamp']:<20} {r['room']:<20} {r['room_type']:<12} "
            f"{str(r['style_id']):<6} {r['status']:<10} {lat:<8} {r['items_count']:<7}"
        )
    print("=" * 110 + "\n")


if __name__ == "__main__":
    init_db()
    print_db_errors()
    print_db_results()
