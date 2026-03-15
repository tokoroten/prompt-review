#!/usr/bin/env python3
"""
prompt-review スキル用データ収集スクリプト

対話履歴を各AIツールから収集し、分析用に整形して標準出力にJSON形式で出力する。

使い方:
    python collect.py                          # 全プロジェクト、全期間
    python collect.py --days 30                # 過去30日分
    python collect.py --project yonshogen      # 特定プロジェクト
    python collect.py --project yonshogen --days 30
"""

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


# クレデンシャル・シークレット検出パターン
SECRET_PATTERNS = [
    (r'(?i)(api[_-]?key|apikey)\s*[:=]\s*\S+', "API Key"),
    (r'(?i)(secret|token|password|passwd|pwd)\s*[:=]\s*\S+', "Secret/Token/Password"),
    (r'(?i)(access[_-]?key|secret[_-]?key)\s*[:=]\s*\S+', "Access Key"),
    (r'(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*', "Bearer Token"),
    (r'sk-[A-Za-z0-9]{20,}', "OpenAI API Key"),
    (r'sk-ant-[A-Za-z0-9\-]{20,}', "Anthropic API Key"),
    (r'ghp_[A-Za-z0-9]{36,}', "GitHub Personal Access Token"),
    (r'gho_[A-Za-z0-9]{36,}', "GitHub OAuth Token"),
    (r'AIza[A-Za-z0-9\-_]{35}', "Google API Key"),
    (r'(?i)aws[_-]?(access|secret)[_-]?key\S*\s*[:=]\s*\S+', "AWS Key"),
    (r'xox[bpras]-[A-Za-z0-9\-]{10,}', "Slack Token"),
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----', "Private Key"),
    (r'(?i)(mongodb(\+srv)?://)\S+:\S+@', "MongoDB Connection String"),
    (r'(?i)(postgres(ql)?://)\S+:\S+@', "PostgreSQL Connection String"),
    (r'(?i)(mysql://)\S+:\S+@', "MySQL Connection String"),
]
_compiled_patterns = [(re.compile(p), label) for p, label in SECRET_PATTERNS]


def scan_secrets(text: str) -> list[dict]:
    """テキスト内のクレデンシャル・シークレットを検出する"""
    findings = []
    for pattern, label in _compiled_patterns:
        for match in pattern.finditer(text):
            matched = match.group()
            # マスク処理: 先頭8文字 + *** + 末尾4文字（短い場合は全体マスク）
            if len(matched) > 16:
                masked = matched[:8] + "***" + matched[-4:]
            else:
                masked = matched[:4] + "***"
            findings.append({
                "type": label,
                "masked_value": masked,
            })
    return findings


def get_appdata_path() -> Path:
    """OSに応じたAppDataパスを返す"""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support"
    else:
        return Path.home() / ".config"


def get_claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def ts_to_iso(ts_ms: int) -> str:
    """Unix epoch ミリ秒をISO 8601文字列に変換"""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "unknown"


def iso_to_ms(iso_str: str) -> int | None:
    """ISO 8601タイムスタンプをUnix epoch ミリ秒に変換"""
    try:
        # "2026-03-12T05:31:13.875Z" 形式
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def sanitize_text(text: str) -> str:
    """サロゲート文字等のJSON非互換文字を除去する"""
    # サロゲートペアの孤立した半分を除去
    return text.encode("utf-8", errors="replace").decode("utf-8")


def extract_user_text(content) -> str:
    """セッションJSONLのmessage.contentからユーザーテキストを抽出する"""
    if isinstance(content, str):
        return sanitize_text(content.strip())
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                # XMLタグで囲まれたシステムメッセージをスキップ
                if re.match(r'^<(ide_opened_file|ide_selection|local-command-caveat|local-command-stdout|system-reminder)\b', text):
                    continue
                # tool_resultタイプもスキップ
                if item.get("type") == "tool_result":
                    continue
                parts.append(text)
            elif isinstance(item, dict) and item.get("type") == "tool_result":
                continue
        return sanitize_text(" ".join(parts).strip())
    return ""


def collect_claude_code(cutoff_ms: int | None, project_filter: str | None) -> dict:
    """Claude Code の history.jsonl およびプロジェクト別セッションファイルからユーザープロンプトを収集"""
    result = {"tool": "Claude Code", "status": "未検出", "messages": [], "period": ""}
    claude_dir = get_claude_dir()

    messages = []
    seen_texts = set()  # 重複排除用
    skip_patterns = ["/clear", "/help"]

    # --- ソース1: history.jsonl（CLI使用時のログ） ---
    history_path = claude_dir / "history.jsonl"
    collected_session_ids = set()

    if history_path.exists():
        with open(history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                display = entry.get("display", "").strip()
                timestamp = entry.get("timestamp")
                project = entry.get("project", "")
                session_id = entry.get("sessionId", "")

                if session_id:
                    collected_session_ids.add(session_id)

                # フィルタ: 空、/clear等、パスのみ
                if not display:
                    continue
                if any(display.startswith(p) for p in skip_patterns):
                    continue
                # 1行でパスっぽいものだけをスキップ
                if display.count("\n") == 0 and len(display) < 300:
                    stripped = display.replace("\\", "/")
                    if stripped.startswith(("/", "C:", "D:", "c:", "d:")) and " " not in stripped and len(stripped.split("/")) > 2:
                        continue

                # タイムスタンプフィルタ
                if cutoff_ms and timestamp and timestamp < cutoff_ms:
                    continue

                # プロジェクトフィルタ
                if project_filter:
                    project_name = Path(project).name.lower() if project else ""
                    if project_filter.lower() not in project_name:
                        continue

                dedup_key = f"{timestamp}:{display[:100]}"
                seen_texts.add(dedup_key)

                messages.append({
                    "text": display[:500],
                    "timestamp": ts_to_iso(timestamp) if timestamp else "unknown",
                    "timestamp_ms": timestamp or 0,
                    "project": Path(project).name if project else "unknown",
                })

    # --- ソース2: プロジェクト別セッションJSONL（VS Code拡張機能のログ） ---
    projects_dir = claude_dir / "projects"
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            # プロジェクトフィルタ: ディレクトリ名からプロジェクト名を復元
            # 例: "c--Users-shinta-Documents-GitHub-yonshogen" → 末尾部分を取得
            dir_name = project_dir.name
            project_name_from_dir = dir_name.rsplit("-", 1)[-1] if "-" in dir_name else dir_name
            if project_filter and project_filter.lower() not in project_name_from_dir.lower():
                # より正確なマッチのため、ディレクトリ名全体でも確認
                if project_filter.lower().replace(" ", "-") not in dir_name.lower():
                    continue

            # セッションJSONLファイルを走査（最新50件に制限）
            session_files = sorted(
                [f for f in project_dir.glob("*.jsonl") if f.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:50]

            for session_file in session_files:
                session_id = session_file.stem
                # history.jsonlで既に収集済みのセッションはスキップ
                if session_id in collected_session_ids:
                    continue

                # カットオフフィルタ: ファイル更新日時で粗くフィルタ
                if cutoff_ms:
                    file_mtime_ms = int(session_file.stat().st_mtime * 1000)
                    if file_mtime_ms < cutoff_ms:
                        continue

                try:
                    msg_count = 0
                    with open(session_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            # ユーザーメッセージのみ抽出
                            if entry.get("type") != "user":
                                continue
                            # メタメッセージ（システム注入）はスキップ
                            if entry.get("isMeta"):
                                continue

                            message = entry.get("message", {})
                            content = message.get("content", "")
                            text = extract_user_text(content)

                            if not text:
                                continue
                            if any(text.startswith(p) for p in skip_patterns):
                                continue

                            # タイムスタンプ処理（ISO 8601形式）
                            ts_str = entry.get("timestamp", "")
                            ts_ms = iso_to_ms(ts_str) if ts_str else None
                            if cutoff_ms and ts_ms and ts_ms < cutoff_ms:
                                continue

                            ts_display = ts_to_iso(ts_ms) if ts_ms else "unknown"

                            # 重複排除
                            dedup_key = f"{ts_ms}:{text[:100]}"
                            if dedup_key in seen_texts:
                                continue
                            seen_texts.add(dedup_key)

                            # cwdからプロジェクト名を取得
                            cwd = entry.get("cwd", "")
                            proj_name = Path(cwd).name if cwd else project_name_from_dir

                            messages.append({
                                "text": text[:500],
                                "timestamp": ts_display,
                                "timestamp_ms": ts_ms or 0,
                                "project": proj_name,
                            })
                            msg_count += 1
                            if msg_count >= 100:
                                break
                except (OSError, UnicodeDecodeError):
                    continue

    if messages:
        result["status"] = "検出"
        result["messages"] = messages
        timestamps = [m["timestamp"] for m in messages if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_copilot_chat(cutoff_ms: int | None, project_filter: str | None) -> dict:
    """GitHub Copilot Chat の state.vscdb からプロンプトを収集"""
    result = {"tool": "GitHub Copilot Chat", "status": "未検出", "messages": [], "period": ""}

    appdata = get_appdata_path()
    workspace_storage = appdata / "Code" / "User" / "workspaceStorage"
    if not workspace_storage.exists():
        return result

    all_prompts = []

    for vscdb_path in workspace_storage.glob("*/state.vscdb"):
        # 接続前にカットオフチェックして I/O を削減
        file_mtime_ms = int(vscdb_path.stat().st_mtime * 1000)
        if cutoff_ms and file_mtime_ms < cutoff_ms:
            continue

        try:
            with sqlite3.connect(str(vscdb_path)) as conn:
                cursor = conn.cursor()

                # memento/interactive-session にプロンプト履歴がある
                cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'memento/interactive-session'"
                )
                row = cursor.fetchone()
                if row and row[0]:
                    try:
                        data = json.loads(row[0])
                        history = data.get("history", {})
                        for mode_key, entries in history.items():
                            if isinstance(entries, list):
                                for entry in entries:
                                    text = entry.get("text", "").strip()
                                    if text:
                                        all_prompts.append({
                                            "text": text[:500],
                                            "timestamp": ts_to_iso(file_mtime_ms),
                                            "timestamp_ms": file_mtime_ms,
                                            "project": vscdb_path.parent.name[:12],
                                        })
                    except (json.JSONDecodeError, AttributeError):
                        pass
        except sqlite3.Error:
            continue

    if all_prompts:
        result["status"] = "検出"
        result["messages"] = all_prompts
        timestamps = [m["timestamp"] for m in all_prompts if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_cursor(cutoff_ms: int | None, project_filter: str | None) -> dict:
    """Cursor の state.vscdb から aiService.prompts を収集"""
    result = {"tool": "Cursor", "status": "未検出", "messages": [], "period": ""}

    appdata = get_appdata_path()
    workspace_storage = appdata / "Cursor" / "User" / "workspaceStorage"
    if not workspace_storage.exists():
        return result

    all_prompts = []

    for vscdb_path in workspace_storage.glob("*/state.vscdb"):
        # 接続前にカットオフチェックして I/O を削減
        file_mtime_ms = int(vscdb_path.stat().st_mtime * 1000)
        if cutoff_ms and file_mtime_ms < cutoff_ms:
            continue

        try:
            with sqlite3.connect(str(vscdb_path)) as conn:
                cur = conn.cursor()

                # aiService.prompts にユーザープロンプト履歴がある
                cur.execute("SELECT value FROM ItemTable WHERE key = 'aiService.prompts'")
                row = cur.fetchone()
                if row and row[0]:
                    try:
                        entries = json.loads(row[0])
                        if not isinstance(entries, list):
                            continue

                        # workspace.json からプロジェクトパスを取得
                        workspace_dir = vscdb_path.parent
                        workspace_json = workspace_dir / "workspace.json"
                        project_name = workspace_dir.name[:12]
                        if workspace_json.exists():
                            try:
                                ws_data = json.loads(workspace_json.read_text(encoding="utf-8"))
                                folder = ws_data.get("folder", "")
                                if folder.startswith("file://"):
                                    folder = folder[7:]
                                project_name = Path(folder).name if folder else project_name
                            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                                pass

                        if project_filter:
                            if project_filter.lower() not in project_name.lower():
                                continue

                        for entry in entries:
                            if isinstance(entry, dict):
                                text = entry.get("text", "").strip()
                                if text:
                                    all_prompts.append({
                                        "text": text[:500],
                                        "timestamp": ts_to_iso(file_mtime_ms),
                                        "timestamp_ms": file_mtime_ms,
                                        "project": project_name,
                                    })
                    except (json.JSONDecodeError, AttributeError):
                        pass
        except sqlite3.Error:
            continue

    if all_prompts:
        result["status"] = "検出"
        result["messages"] = all_prompts
        timestamps = [m["timestamp"] for m in all_prompts if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_cline(cutoff_ms: int | None) -> dict:
    """Cline の api_conversation_history.json からプロンプトを収集"""
    result = {"tool": "Cline", "status": "未検出", "messages": [], "period": ""}

    appdata = get_appdata_path()
    tasks_dir = appdata / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks"
    if not tasks_dir.exists():
        return result

    all_prompts = []
    task_dirs = sorted(tasks_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:20]

    for task_dir in task_dirs:
        history_file = task_dir / "api_conversation_history.json"
        if not history_file.exists():
            continue

        if cutoff_ms:
            file_mtime_ms = int(history_file.stat().st_mtime * 1000)
            if file_mtime_ms < cutoff_ms:
                continue

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for msg in data:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text = " ".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        )
                    else:
                        text = str(content)
                    text = text.strip()
                    if text:
                        file_mtime_ms = int(history_file.stat().st_mtime * 1000)
                        all_prompts.append({
                            "text": text[:500],
                            "timestamp": ts_to_iso(file_mtime_ms),
                            "timestamp_ms": file_mtime_ms,
                            "project": task_dir.name[:12],
                        })
        except (json.JSONDecodeError, OSError):
            continue

    if all_prompts:
        result["status"] = "検出"
        result["messages"] = all_prompts
        timestamps = [m["timestamp"] for m in all_prompts if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_roo_code(cutoff_ms: int | None) -> dict:
    """Roo Code の会話履歴を収集（Clineと同じ構造）"""
    result = {"tool": "Roo Code", "status": "未検出", "messages": [], "period": ""}

    appdata = get_appdata_path()
    tasks_dir = appdata / "Code" / "User" / "globalStorage" / "RooVeterinaryInc.roo-cline" / "tasks"
    if not tasks_dir.exists():
        return result

    # Clineと同じ処理
    all_prompts = []
    task_dirs = sorted(tasks_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:20]

    for task_dir in task_dirs:
        history_file = task_dir / "api_conversation_history.json"
        if not history_file.exists():
            continue

        if cutoff_ms:
            file_mtime_ms = int(history_file.stat().st_mtime * 1000)
            if file_mtime_ms < cutoff_ms:
                continue

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for msg in data:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text = " ".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        )
                    else:
                        text = str(content)
                    text = text.strip()
                    if text:
                        file_mtime_ms = int(history_file.stat().st_mtime * 1000)
                        all_prompts.append({
                            "text": text[:500],
                            "timestamp": ts_to_iso(file_mtime_ms),
                            "timestamp_ms": file_mtime_ms,
                            "project": task_dir.name[:12],
                        })
        except (json.JSONDecodeError, OSError):
            continue

    if all_prompts:
        result["status"] = "検出"
        result["messages"] = all_prompts
        timestamps = [m["timestamp"] for m in all_prompts if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_windsurf(cutoff_ms: int | None) -> dict:
    """Windsurf のメモリファイルを収集"""
    result = {"tool": "Windsurf", "status": "未検出", "messages": [], "period": ""}

    memories_dir = Path.home() / ".codeium" / "windsurf" / "memories"
    if not memories_dir.exists():
        return result

    all_entries = []
    for mem_file in sorted(memories_dir.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        if not mem_file.is_file():
            continue
        if cutoff_ms:
            file_mtime_ms = int(mem_file.stat().st_mtime * 1000)
            if file_mtime_ms < cutoff_ms:
                continue
        try:
            text = mem_file.read_text(encoding="utf-8").strip()
            if text:
                file_mtime_ms = int(mem_file.stat().st_mtime * 1000)
                all_entries.append({
                    "text": text[:500],
                    "timestamp": ts_to_iso(file_mtime_ms),
                    "timestamp_ms": file_mtime_ms,
                    "project": mem_file.parent.name,
                    "note": "Cascadeの自動要約メモリ（元のプロンプトではない）",
                })
        except (OSError, UnicodeDecodeError):
            continue

    if all_entries:
        result["status"] = "検出"
        result["messages"] = all_entries
        timestamps = [m["timestamp"] for m in all_entries if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_antigravity(cutoff_ms: int | None) -> dict:
    """Google Antigravity のログを収集"""
    result = {"tool": "Google Antigravity", "status": "未検出", "messages": [], "period": ""}

    brain_dir = Path.home() / ".gemini" / "antigravity" / "brain"
    if not brain_dir.exists():
        return result

    all_entries = []
    for log_dir in brain_dir.glob("*/.system_generated/logs"):
        for log_file in sorted(log_dir.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            if not log_file.is_file() or log_file.suffix == ".pb":
                continue
            if cutoff_ms:
                file_mtime_ms = int(log_file.stat().st_mtime * 1000)
                if file_mtime_ms < cutoff_ms:
                    continue
            try:
                text = log_file.read_text(encoding="utf-8").strip()
                if text:
                    file_mtime_ms = int(log_file.stat().st_mtime * 1000)
                    all_entries.append({
                        "text": text[:500],
                        "timestamp": ts_to_iso(file_mtime_ms),
                        "timestamp_ms": file_mtime_ms,
                        "project": log_file.parent.parent.parent.name[:12],
                    })
            except (OSError, UnicodeDecodeError):
                continue

    if all_entries:
        result["status"] = "検出"
        result["messages"] = all_entries
        timestamps = [m["timestamp"] for m in all_entries if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_gemini_cli(cutoff_ms: int | None, project_filter: str | None) -> dict:
    """Gemini CLI の会話履歴を収集（~/.gemini/tmp/<project_name_or_hash>/chats/ 配下）

    新形式（v0.29+ 相当）: chats/session-*.json が JSON オブジェクト
        {sessionId, projectHash, startTime, lastUpdated, messages: [{id, timestamp, type, content}]}
    旧形式（JSONL）: chats/session-*.jsonl が1行1エントリの JSONL
    旧形式（手動保存）: chats/checkpoint-*.json が JSON 配列 [{role, parts}]
    """
    result = {"tool": "Gemini CLI", "status": "未検出", "messages": [], "period": ""}

    gemini_tmp = Path.home() / ".gemini" / "tmp"
    if not gemini_tmp.exists():
        return result

    all_messages = []

    for project_dir in sorted(gemini_tmp.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not project_dir.is_dir():
            continue

        chats_dir = project_dir / "chats"
        if not chats_dir.exists():
            continue

        project_name = project_dir.name

        # project_filter: 新形式ではディレクトリ名がプロジェクト名（例: "gemini-cli"）のため部分一致フィルタを適用。
        # ディレクトリ名がハッシュ値（64文字の16進数）の場合は逆引き不可のためスキップしない。
        is_hash_dir = len(project_name) == 64 and all(c in "0123456789abcdef" for c in project_name.lower())
        if project_filter and not is_hash_dir and project_filter.lower() not in project_name.lower():
            continue

        # 最新20件のチャットファイルを走査（.json / .jsonl 両対応）
        chat_files = sorted(
            list(chats_dir.glob("*.jsonl")) + list(chats_dir.glob("*.json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:20]

        for chat_file in chat_files:
            file_mtime_ms = int(chat_file.stat().st_mtime * 1000)
            if cutoff_ms and file_mtime_ms < cutoff_ms:
                continue

            try:
                if chat_file.suffix == ".jsonl":
                    # 旧形式（JSONL）: 1行1エントリ
                    # 各行: session_metadata | user | gemini | message_update
                    session_start_ms = None
                    msg_count = 0
                    with open(chat_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            entry_type = entry.get("type", "")

                            if entry_type == "session_metadata":
                                start_time = entry.get("startTime", "")
                                if start_time:
                                    session_start_ms = iso_to_ms(start_time)
                                continue

                            if entry_type != "user":
                                continue

                            content = entry.get("content", [])
                            texts = []
                            if isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict):
                                        text = part.get("text", "").strip()
                                        if text:
                                            texts.append(text)

                            text = sanitize_text(" ".join(texts).strip())
                            if not text:
                                continue

                            ts_ms = session_start_ms or file_mtime_ms
                            if cutoff_ms and ts_ms < cutoff_ms:
                                continue

                            all_messages.append({
                                "text": text[:500],
                                "timestamp": ts_to_iso(ts_ms),
                                "timestamp_ms": ts_ms,
                                "project": project_name,
                            })
                            msg_count += 1
                            if msg_count >= 100:
                                break
                else:
                    # .json ファイル: 新形式（セッションオブジェクト）または旧形式（配列）を判別
                    with open(chat_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    if isinstance(data, dict) and "messages" in data:
                        # 新形式: {"sessionId": ..., "messages": [{id, timestamp, type, content}]}
                        session_start_time = data.get("startTime", "")
                        session_start_ms = iso_to_ms(session_start_time) if session_start_time else file_mtime_ms
                        msg_count = 0
                        for msg in data["messages"]:
                            if not isinstance(msg, dict):
                                continue
                            if msg.get("type") != "user":
                                continue

                            # メッセージ単位のタイムスタンプ（ISO 8601）を優先
                            msg_ts_str = msg.get("timestamp", "")
                            ts_ms = (iso_to_ms(msg_ts_str) if msg_ts_str else None) or session_start_ms or file_mtime_ms

                            if cutoff_ms and ts_ms < cutoff_ms:
                                continue

                            content = msg.get("content", [])
                            texts = []
                            if isinstance(content, list):
                                for part in content:
                                    if not isinstance(part, dict):
                                        continue
                                    raw = part.get("text", "").strip()
                                    if not raw:
                                        continue
                                    # @ファイル参照で注入された内容を除去:
                                    # "--- Content from referenced files ---" 以降はシステム注入コンテンツ
                                    if "--- Content from referenced files ---" in raw:
                                        # このパート内で分割（プロンプトテキスト側を取る）
                                        raw = raw.split("--- Content from referenced files ---")[0].strip()
                                        if raw:
                                            texts.append(raw)
                                        break  # 以降のパートも注入コンテンツなのでスキップ
                                    # "--- End of content ---" のみのパートもスキップ
                                    if raw == "--- End of content ---":
                                        break
                                    texts.append(raw)

                            text = sanitize_text(" ".join(texts).strip())
                            if not text:
                                continue

                            all_messages.append({
                                "text": text[:500],
                                "timestamp": ts_to_iso(ts_ms or file_mtime_ms),
                                "timestamp_ms": ts_ms or file_mtime_ms,
                                "project": project_name,
                            })
                            msg_count += 1
                            if msg_count >= 100:
                                break

                    elif isinstance(data, list):
                        # 旧形式（手動保存チェックポイント）: [{role, parts}]
                        msg_count = 0
                        for msg in data:
                            if not isinstance(msg, dict):
                                continue
                            if msg.get("role") != "user":
                                continue
                            parts = msg.get("parts", [])
                            texts = [
                                p.get("text", "").strip()
                                for p in parts
                                if isinstance(p, dict) and p.get("text", "").strip()
                            ]
                            text = sanitize_text(" ".join(texts).strip())
                            if text:
                                all_messages.append({
                                    "text": text[:500],
                                    "timestamp": ts_to_iso(file_mtime_ms),
                                    "timestamp_ms": file_mtime_ms,
                                    "project": project_name,
                                })
                                msg_count += 1
                                if msg_count >= 100:
                                    break

            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue

    if all_messages:
        result["status"] = "検出"
        result["messages"] = all_messages
        timestamps = [m["timestamp"] for m in all_messages if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_codex(cutoff_ms: int | None, project_filter: str | None) -> dict:
    """OpenAI Codex CLI の rollout JSONL からユーザープロンプトを収集"""
    result = {"tool": "OpenAI Codex", "status": "未検出", "messages": [], "period": ""}

    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return result

    messages = []

    # sessions/YYYY/MM/DD/rollout-*.jsonl を走査
    rollout_files = sorted(
        sessions_dir.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:50]

    for rollout_path in rollout_files:
        # カットオフフィルタ: ファイル更新日時で粗くフィルタ
        if cutoff_ms:
            file_mtime_ms = int(rollout_path.stat().st_mtime * 1000)
            if file_mtime_ms < cutoff_ms:
                continue

        try:
            cwd = ""
            session_messages = []
            msg_count = 0

            with open(rollout_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = entry.get("type", "")
                    payload = entry.get("payload")
                    if not entry_type or not isinstance(payload, dict):
                        continue

                    timestamp_str = entry.get("timestamp", "")

                    # session_meta からプロジェクト情報を取得
                    if entry_type == "session_meta":
                        cwd = payload.get("cwd", "")
                        continue

                    # response_item からユーザーメッセージを抽出
                    if entry_type != "response_item":
                        continue
                    response_item = payload

                    item_type = response_item.get("type", "")
                    role = response_item.get("role", "")
                    if item_type != "message" or role != "user":
                        continue

                    # コンテンツからテキストを抽出
                    content = response_item.get("content", [])
                    texts = []
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                part_type = part.get("type", "")
                                if part_type in ("input_text", "text"):
                                    text = part.get("text", "").strip()
                                    if text:
                                        texts.append(text)
                    elif isinstance(content, str):
                        texts.append(content.strip())

                    text = sanitize_text(" ".join(texts).strip())
                    if not text:
                        continue

                    # タイムスタンプ処理
                    ts_ms = iso_to_ms(timestamp_str) if timestamp_str else None
                    if cutoff_ms and ts_ms and ts_ms < cutoff_ms:
                        continue

                    ts_display = ts_to_iso(ts_ms) if ts_ms else "unknown"

                    session_messages.append({
                        "text": text[:500],
                        "timestamp": ts_display,
                        "timestamp_ms": ts_ms or 0,
                        "_cwd_placeholder": True,  # cwdは後で設定
                    })
                    msg_count += 1
                    if msg_count >= 100:
                        break

            # プロジェクト名を設定
            project_name = Path(cwd).name if cwd else "unknown"

            # プロジェクトフィルタ
            if project_filter:
                filter_lower = project_filter.lower()
                if filter_lower not in project_name.lower() and (not cwd or filter_lower not in cwd.lower()):
                    continue

            for msg in session_messages:
                del msg["_cwd_placeholder"]
                msg["project"] = project_name
                messages.append(msg)

        except (OSError, UnicodeDecodeError):
            continue

    if messages:
        result["status"] = "検出"
        result["messages"] = messages
        timestamps = [m["timestamp"] for m in messages if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def collect_opencode(cutoff_ms: int | None, project_filter: str | None) -> dict:
    """OpenCode の SQLite DB からユーザープロンプトを収集"""
    result = {"tool": "OpenCode", "status": "未検出", "messages": [], "period": ""}

    xdg_data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    opencode_dir = xdg_data_home / "opencode"
    if not opencode_dir.exists():
        return result

    db_paths = []
    primary_db = opencode_dir / "opencode.db"
    if primary_db.exists():
        db_paths.append(primary_db)

    for db_path in sorted(opencode_dir.glob("opencode-*.db")):
        if db_path not in db_paths:
            db_paths.append(db_path)

    if not db_paths:
        return result

    messages = []
    seen_message_ids = set()

    for db_path in db_paths:
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    m.id AS message_id,
                    m.time_created AS message_time_created,
                    m.data AS message_data,
                    p.worktree AS project_worktree,
                    s.directory AS session_directory,
                    s.parent_id AS session_parent_id,
                    pt.id AS part_id,
                    pt.time_created AS part_time_created,
                    pt.data AS part_data
                FROM message m
                JOIN session s ON s.id = m.session_id
                JOIN project p ON p.id = s.project_id
                JOIN part pt ON pt.message_id = m.id
                ORDER BY m.time_created ASC, pt.time_created ASC, pt.id ASC
                """
            )

            grouped = {}
            for row in cursor.fetchall():
                message_id = row["message_id"]
                if message_id in seen_message_ids:
                    continue

                if message_id not in grouped:
                    try:
                        message_data = json.loads(row["message_data"])
                    except (TypeError, json.JSONDecodeError):
                        grouped[message_id] = None
                        continue

                    if row["session_parent_id"] is not None:
                        grouped[message_id] = None
                        continue

                    if message_data.get("role") != "user":
                        grouped[message_id] = None
                        continue

                    grouped[message_id] = {
                        "timestamp_ms": row["message_time_created"] or 0,
                        "worktree": row["project_worktree"] or "",
                        "session_directory": row["session_directory"] or "",
                        "texts": [],
                    }

                entry = grouped.get(message_id)
                if entry is None:
                    continue

                try:
                    part_data = json.loads(row["part_data"])
                except (TypeError, json.JSONDecodeError):
                    continue

                if part_data.get("type") != "text":
                    continue
                if part_data.get("synthetic") is True:
                    continue
                if part_data.get("ignored") is True:
                    continue

                text = sanitize_text(str(part_data.get("text", "")).strip())
                if not text:
                    continue
                entry["texts"].append(text)

            for message_id, entry in grouped.items():
                if entry is None:
                    continue

                text = " ".join(entry["texts"]).strip()
                if not text:
                    continue

                timestamp_ms = entry["timestamp_ms"]
                if cutoff_ms and timestamp_ms and timestamp_ms < cutoff_ms:
                    continue

                project_source = entry["worktree"] or entry["session_directory"]
                project_name = Path(project_source).name if project_source else "unknown"

                if project_filter:
                    filter_value = project_filter.lower()
                    haystacks = [project_name.lower()]
                    if entry["worktree"]:
                        haystacks.append(entry["worktree"].lower())
                    if entry["session_directory"]:
                        haystacks.append(entry["session_directory"].lower())
                    if not any(filter_value in item for item in haystacks):
                        continue

                messages.append({
                    "text": text[:500],
                    "timestamp": ts_to_iso(timestamp_ms) if timestamp_ms else "unknown",
                    "timestamp_ms": timestamp_ms,
                    "project": project_name or "unknown",
                })
                seen_message_ids.add(message_id)
        except sqlite3.Error:
            continue
        finally:
            if conn:
                conn.close()

    if messages:
        result["status"] = "検出"
        result["messages"] = messages
        timestamps = [m["timestamp"] for m in messages if m["timestamp"] != "unknown"]
        if timestamps:
            result["period"] = f"{min(timestamps)} 〜 {max(timestamps)}"

    return result


def main():
    parser = argparse.ArgumentParser(description="AI対話履歴を収集・整形して出力する")
    parser.add_argument("--days", type=int, default=7, help="過去N日分に限定（デフォルト: 7日）")
    parser.add_argument("--project", type=str, default=None, help="プロジェクト名でフィルタ（部分一致）")
    args = parser.parse_args()

    # カットオフ算出
    cutoff_ms = None
    if args.days and args.days > 0:
        cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
        cutoff_ms = int(cutoff_dt.timestamp() * 1000)

    # 各ソースから収集
    sources = [
        collect_claude_code(cutoff_ms, args.project),
        collect_copilot_chat(cutoff_ms, args.project),
        collect_cursor(cutoff_ms, args.project),
        collect_cline(cutoff_ms),
        collect_roo_code(cutoff_ms),
        collect_windsurf(cutoff_ms),
        collect_antigravity(cutoff_ms),
        collect_gemini_cli(cutoff_ms, args.project),
        collect_codex(cutoff_ms, args.project),
        collect_opencode(cutoff_ms, args.project),
    ]

    # サマリー生成
    total_messages = sum(len(s["messages"]) for s in sources)
    detected = [s["tool"] for s in sources if s["status"] == "検出"]

    output = {
        "summary": {
            "total_messages": total_messages,
            "detected_tools": detected,
            "filter_days": args.days,
            "filter_project": args.project,
            "collected_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
        "sources": sources,
    }

    # シークレット検出
    secret_warnings = []
    for source in sources:
        for msg in source["messages"]:
            findings = scan_secrets(msg["text"])
            if findings:
                for f in findings:
                    secret_warnings.append({
                        "tool": source["tool"],
                        "project": msg.get("project", "unknown"),
                        "timestamp": msg.get("timestamp", "unknown"),
                        "type": f["type"],
                        "masked_value": f["masked_value"],
                        "prompt_excerpt": msg["text"][:80].replace("\n", " "),
                    })
    output["secret_warnings"] = secret_warnings

    # プロジェクト別集計
    project_stats = {}
    for source in sources:
        for msg in source["messages"]:
            proj = msg.get("project", "unknown")
            if proj not in project_stats:
                project_stats[proj] = {"count": 0, "tools": set()}
            project_stats[proj]["count"] += 1
            project_stats[proj]["tools"].add(source["tool"])
    # setはJSON化できないのでlistに変換
    output["project_stats"] = {
        k: {"count": v["count"], "tools": list(v["tools"])}
        for k, v in sorted(project_stats.items(), key=lambda x: -x[1]["count"])
    }

    # Windows環境でのUTF-8出力を保証
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
