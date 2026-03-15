# データソース詳細リファレンス

各AIツールのログ保存場所・フォーマット・抽出方法の詳細。

---

## 1. Claude Code（主要ソース）

### 保存場所
| OS | パス |
|----|------|
| Windows | `C:\Users\<user>\.claude\` |
| macOS | `~/.claude/` |
| Linux | `~/.claude/` |

環境変数 `CLAUDE_CONFIG_DIR` が設定されている場合は、そのパスを使用する。

### ファイル構造
```
~/.claude/
├── history.jsonl                          # 全プロジェクトの軽量インデックス
└── projects/
    └── {encoded-path}/                    # パス区切り(\, /, :) → - に変換
        ├── {session-uuid}.jsonl           # セッション別の完全な会話ログ
        └── {session-uuid}/
            └── subagents/                 # サブエージェントのログ
```

### history.jsonl の形式
```json
{"display":"ユーザーの入力テキスト","pastedContents":{},"timestamp":1759115795246,"project":"D:\\path\\to\\project"}
```

### プロジェクト別JSONL の形式
各行がJSON。ユーザーメッセージは `"type":"user"` を含む。
```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]},"timestamp":"..."}
```

### 抽出方法（2ソース方式）

**ソース1: history.jsonl（CLI使用時）**
1. `history.jsonl` を読み込み、`display` フィールドからプロンプトを取得
2. `/clear` やファイルパスのみの行を除外
3. 収集済みの `sessionId` を記録（ソース2との重複排除用）

**ソース2: プロジェクト別セッションJSONL（VS Code拡張機能使用時）**
1. `projects/` 配下の各プロジェクトディレクトリを走査
2. 各ディレクトリの `*.jsonl` ファイルから `"type":"user"` のメッセージを抽出
3. `isMeta: true` のシステムメッセージはスキップ
4. `<ide_opened_file>`, `<local-command-stdout>` 等のシステムタグを除外
5. history.jsonl で収集済みのセッションIDはスキップ（重複排除）
6. 各プロジェクト最新50ファイル、1ファイルあたりユーザーメッセージ100件上限

**重要**: VS Code拡張機能で使用した場合、`history.jsonl` にはエントリが記録されず、
プロジェクト別セッションJSONLにのみ会話ログが保存される。両方のソースを読む必要がある。

### パスエンコード規則
プロジェクトディレクトリ名は、元のパスの `\`, `/`, `:` を `-` に変換。
例: `C:\Users\shinta\Documents\GitHub\yonshogen` → `c--Users-shinta-Documents-GitHub-yonshogen`

---

## 2. GitHub Copilot Chat

### 保存場所
| OS | パス |
|----|------|
| Windows | `%APPDATA%\Code\User\workspaceStorage\*\state.vscdb` |
| macOS | `~/Library/Application Support/Code/User/workspaceStorage/*/state.vscdb` |
| Linux | `~/.config/Code/User/workspaceStorage/*/state.vscdb` |

### ファイル形式
SQLite データベース（`state.vscdb`）

### 抽出方法
```bash
# 利用可能なキーの一覧を取得
sqlite3 "<path>/state.vscdb" "SELECT key FROM ItemTable WHERE key LIKE '%chat%';"

# チャットセッションインデックスを取得
sqlite3 "<path>/state.vscdb" "SELECT value FROM ItemTable WHERE key = 'interactive.sessions';" 2>/dev/null

# 代替キー（バージョンにより異なる）
sqlite3 "<path>/state.vscdb" "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index';" 2>/dev/null
```

### 注意事項
- `sqlite3` コマンドが必要（Windows では Git Bash 付属のものや別途インストール）
- ワークスペースごとに別の `state.vscdb` が存在する
- セッションデータはJSON文字列としてvalueカラムに格納
- ユーザーのプロンプトは `request` や `message` フィールドに含まれる

---

## 3. Cursor

### 保存場所
| OS | パス |
|----|------|
| Windows | `%APPDATA%\Cursor\User\workspaceStorage\*\state.vscdb` |
| macOS | `~/Library/Application Support/Cursor/User/workspaceStorage/*/state.vscdb` |
| Linux | `~/.config/Cursor/User/workspaceStorage/*/state.vscdb` |

### ファイル形式
SQLite データベース（`state.vscdb`）。VS Code 系と同様の workspaceStorage 構造。

### ファイル構造
```
Cursor/User/workspaceStorage/
├── {workspace-id}/
│   ├── state.vscdb           # SQLite DB（チャット履歴含む）
│   └── workspace.json        # ワークスペースパス（folder: file:///path/to/project）
```

### 抽出方法
1. `aiService.prompts` キーからプロンプト履歴を取得
2. 形式: `[{"text": "ユーザーの入力", "commandType": 4}, ...]`
3. `workspace.json` の `folder` からプロジェクト名を取得（file:// プレフィックスを除去）
4. タイムスタンプは state.vscdb の更新日時を代用（プロンプト単位のタイムスタンプは非公開）

```bash
sqlite3 "<path>/state.vscdb" "SELECT value FROM ItemTable WHERE key = 'aiService.prompts';"
```

### 注意事項
- Cursor v0.43 以降では `composer.composerData` にも会話メタデータがあるが、プロンプト本文は `aiService.prompts` に格納
- データベース形式は非公式のため、将来のバージョンで変更される可能性がある

---

## 4. Cline

### 保存場所
| OS | パス |
|----|------|
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\` |
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/` |
| Linux | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/` |

### ファイル構造
```
saoudrizwan.claude-dev/
├── state/
│   └── taskHistory.json              # タスク履歴インデックス
└── tasks/
    └── {task-id}/
        ├── api_conversation_history.json  # API会話ログ
        ├── ui_messages.json               # UI表示メッセージ
        └── task_metadata.json             # タスクメタデータ
```

### 抽出方法
1. `taskHistory.json` を読み込んでタスク一覧を取得
2. 各タスクの `api_conversation_history.json` から `role: "human"` のメッセージを抽出
3. サンプリング: 最新20タスクに制限

---

## 5. Roo Code

### 保存場所
| OS | パス |
|----|------|
| Windows | `%APPDATA%\Code\User\globalStorage\RooVeterinaryInc.roo-cline\` |
| macOS | `~/Library/Application Support/Code/User/globalStorage/RooVeterinaryInc.roo-cline/` |
| Linux | `~/.config/Code/User/globalStorage/RooVeterinaryInc.roo-cline/` |

### ファイル構造
Cline と同一構造（Roo Code は Cline のフォーク）。
```
RooVeterinaryInc.roo-cline/
├── state/
│   └── taskHistory.json
└── tasks/
    └── {task-id}/
        ├── api_conversation_history.json
        ├── ui_messages.json
        └── task_metadata.json
```

### 抽出方法
Cline と同じ手順。

---

## 6. Windsurf (Cascade)

### 保存場所
| OS | パス |
|----|------|
| Windows | `%USERPROFILE%\.codeium\windsurf\memories\` |
| macOS | `~/.codeium/windsurf/memories/` |
| Linux | `~/.codeium/windsurf/memories/` |

バックアップ（cascade-backup-utils 使用時）:
`~/.cascade_backups/`

### ファイル形式
メモリファイル（テキスト形式）。会話の直接ログではなく、Cascadeが自動生成した要約・メモリ。

### 抽出方法
1. `~/.codeium/windsurf/memories/` 配下をGlobで検索
2. テキストファイルをReadで読み込み
3. `~/.cascade_backups/` が存在する場合はそちらも読み込み
4. メモリファイルのため、直接のプロンプトではなくCascadeの要約情報として扱う

### 注意事項
- Windsurf の会話ログ自体はローカルに直接保存されない場合がある
- memories/ はワークスペース単位で分離されている
- メモリの内容は要約であり、元のプロンプトとは異なる

---

## 7. Google Antigravity

### 保存場所
| OS | パス |
|----|------|
| Windows | `%USERPROFILE%\.gemini\antigravity\brain\` |
| macOS | `~/.gemini/antigravity/brain/` + `~/.gemini/antigravity/conversations/` |
| Linux | `~/.gemini/antigravity/brain/` |

### ファイル構造
```
~/.gemini/antigravity/
├── brain/
│   └── {conversation-id}/
│       └── .system_generated/
│           └── logs/                  # 会話ログ
└── conversations/
    └── *.pb                           # Protocol Buffers 形式
```

### 抽出方法
1. `~/.gemini/antigravity/brain/` 配下をGlobで探索
2. `.system_generated/logs/` 内のテキストファイルを読み込み
3. `.pb` ファイル（Protocol Buffers）はバイナリのため直接読み取り不可 → スキップ
4. テキスト形式のログファイルのみ対象

### 注意事項
- Antigravity は比較的新しいツールのため、ログ形式が変更される可能性がある
- `.gemini/` フォルダが削除されると会話リストは残るが内容は読めなくなる（既知のバグ）
- Protocol Buffers 形式のファイルはテキストとして読めないためスキップする

---

## 8. Gemini CLI

### 保存場所
| OS | パス |
|----|------|
| Windows | `%USERPROFILE%\.gemini\tmp\<project_name_or_hash>\chats\` |
| macOS | `~/.gemini/tmp/<project_name_or_hash>/chats/` |
| Linux | `~/.gemini/tmp/<project_name_or_hash>/chats/` |

新しいバージョン（v0.29+ 相当）では `<project_name>` はプロジェクトルートディレクトリのベース名（例: `gemini-cli`）。
古いバージョンでは 64文字の16進数ハッシュ値（逆引き不可）。両形式が混在する場合がある。

### ファイル構造
```
~/.gemini/tmp/
├── {project_name}/               # 新形式: 人間が読めるプロジェクト名
│   └── chats/
│       └── session-YYYY-MM-DDTHH-MM-{id}.json    # セッションファイル（新形式）
└── {project_hash}/               # 旧形式: 64文字16進数ハッシュ
    └── chats/
        ├── session-{id}.jsonl    # 自動セッション記録（旧JSONL形式）
        └── checkpoint-{name}.json  # /chat save による手動保存
```

### ファイル形式

**JSON形式（セッションオブジェクト、新形式）** — `session-*.json` がJSONオブジェクト

```json
{
  "sessionId": "uuid",
  "projectHash": "sha256...",
  "startTime": "2026-03-06T04:16:33.678Z",
  "lastUpdated": "2026-03-06T04:16:39.320Z",
  "messages": [
    {
      "id": "uuid",
      "timestamp": "2026-03-06T04:16:33.678Z",
      "type": "user",
      "content": [
        {"text": "ユーザーの入力"},
        {"text": "\n--- Content from referenced files ---"},
        {"text": "注入されたファイル内容"},
        {"text": "\n--- End of content ---"}
      ]
    },
    {"id": "uuid", "timestamp": "...", "type": "gemini", "content": "AIの応答テキスト"},
    {"id": "uuid", "timestamp": "...", "type": "info", "content": "システム情報"}
  ]
}
```

**JSONL形式（自動セッション、旧形式）** — `session-*.jsonl` が1行1エントリ

各行が以下のいずれかのJSON:
```json
{"type": "session_metadata", "sessionId": "...", "projectHash": "...", "startTime": "2025-06-15T10:30:00Z"}
{"type": "user", "id": "msg1", "content": [{"text": "ユーザーの入力"}]}
{"type": "gemini", "id": "msg2", "content": [{"text": "AIの応答"}]}
{"type": "message_update", "id": "msg2", "tokens": {"input": 10, "output": 5}}
```

**JSON形式（手動保存チェックポイント、旧形式）** — `checkpoint-*.json` がJSON配列

```json
[
  {"role": "user", "parts": [{"text": "ユーザーの入力"}]},
  {"role": "model", "parts": [{"text": "AIの応答"}]}
]
```

### 抽出方法
1. `~/.gemini/tmp/` 配下の全プロジェクトディレクトリを走査
2. 各ディレクトリの `chats/` 配下のファイルを更新日時降順で最新20件取得
3. `.json` ファイルはトップレベルが `dict` かつ `messages` キーを持つ場合は新形式として処理:
   - `type == "user"` のメッセージの `content[].text` を抽出
   - `--- Content from referenced files ---` パート以降はシステム注入コンテンツとして除外
   - メッセージ単位の `timestamp`（ISO 8601）でカットオフフィルタを適用
4. `.json` ファイルがリストの場合は旧形式: `role == "user"` の `parts[].text` を抽出
5. `.jsonl` ファイルは1行ずつパース: `type == "user"` の行の `content[].text` を抽出
6. タイムスタンプが取得できない場合はファイルの更新日時を使用

### 注意事項
- 新形式のプロジェクトディレクトリ名はプロジェクト名（`--project` フィルタで部分一致）
- 旧形式のハッシュ名ディレクトリは逆引き不可のため `--project` フィルタをスキップして全件収集
- デフォルトの保持期間は30日（`~/.gemini/settings.json` の `sessionRetention.maxAge` で変更可能）
- 旧JSONL形式はメッセージ単位のタイムスタンプを持たず、セッション開始時刻を全メッセージに適用

---

## 9. OpenAI Codex（CLI）

### 保存場所
| OS | パス |
|----|------|
| Windows | `%USERPROFILE%\.codex\sessions\` |
| macOS | `~/.codex/sessions/` |
| Linux | `~/.codex/sessions/` |

環境変数 `CODEX_HOME` が設定されている場合は、`$CODEX_HOME/sessions/` を使用する。

### ファイル構造
```
~/.codex/
├── config.toml                                    # 設定ファイル
├── state-v5.db                                    # スレッドメタデータ（SQLite）
├── session_index.jsonl                            # セッションインデックス
└── sessions/
    └── YYYY/MM/DD/
        └── rollout-YYYY-MM-DDThh-mm-ss-<id>.jsonl # セッション別会話ログ
```

### rollout JSONL の形式
各行がJSONオブジェクト。`type` フィールドで行の種別を判別し、データは `payload` に格納する:

**session_meta（セッション開始時のメタ情報）**
```json
{"timestamp": "2025-06-15T10:30:00.123Z", "type": "session_meta", "payload": {"cwd": "/path/to/project", "model_provider": "openai", ...}}
```

**response_item（会話アイテム）**
```json
{"timestamp": "2025-06-15T10:30:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ユーザーの入力"}]}}
```

### 抽出方法
1. `~/.codex/sessions/` 配下の `rollout-*.jsonl` を再帰的に走査（最新50件）
2. 各行の `type` と `payload` を読み取り、`type` が存在しない行はスキップ
3. `session_meta` の `payload.cwd` でプロジェクト情報を取得
4. `response_item` で `payload.type: "message"`, `payload.role: "user"` のメッセージを抽出
5. `content` 配列から `type: "input_text"` または `type: "text"` のパートを連結
6. 1ファイルあたりユーザーメッセージ100件上限

### 注意事項
- セッションはグローバル保存（プロジェクト別ディレクトリではない）
- プロジェクト情報は `session_meta` の `payload.cwd` フィールドから取得
- `state-v5.db` にもスレッドメタデータがあるが、会話内容は rollout JSONL に保存される

---

## 10. OpenCode

### 保存場所
| OS | パス |
|----|------|
| Windows | `%USERPROFILE%\.local\share\opencode\` |
| macOS | `~/.local/share/opencode/` |
| Linux | `~/.local/share/opencode/` |

環境変数 `XDG_DATA_HOME` が設定されている場合は、`$XDG_DATA_HOME/opencode/` を使用する。

### ファイル形式
SQLite データベース（`opencode.db` または `opencode-<channel>.db`）

### 主要テーブル
- `project` - `worktree`, `name` などのプロジェクト情報
- `session` - セッション単位のメタデータ
- `message` - メッセージ単位の情報。`data` カラムはJSON
- `part` - メッセージの各パート。`data` カラムはJSON

### 抽出方法
1. `opencode.db` を優先し、存在しない場合は `opencode-*.db` を探索
2. `project`, `session`, `message`, `part` を結合
3. `session.parent_id IS NULL` の親セッションのみ対象（サブエージェントの子セッションは除外）
4. `message.data.role == "user"` のメッセージのみ対象
5. `part.data.type == "text"` かつ `part.data.synthetic != true` かつ `part.data.ignored != true` のパートを連結してプロンプト化
6. タイムスタンプは `message.time_created`（Unix epoch ミリ秒）を使用
7. プロジェクト名は `project.worktree` の basename を使用

---

## 共通のサンプリング戦略

大量のログデータを効率的に処理するため、以下の戦略を適用する:

| パラメータ | 値 | 理由 |
|-----------|-----|------|
| プロジェクトあたり最大ファイル数 | 5 | コンテキストウィンドウの制約 |
| ファイルあたり最大ユーザーメッセージ数 | 100 | 十分な分析量を確保しつつ制限 |
| Cline/Roo Code 最大タスク数 | 20 | 直近の活動に焦点 |
| 日数フィルタ | 引数で指定 | timestamp を現在時刻と比較 |

### 日数フィルタの適用方法
- Claude Code: `timestamp` フィールド（Unix epoch ミリ秒）で比較
- Cline/Roo Code: `task_metadata.json` のタイムスタンプで比較
- GitHub Copilot Chat: セッションデータ内のタイムスタンプで比較
- Cursor: state.vscdb の更新日時で比較（プロンプト単位のタイムスタンプは非公開）
- OpenAI Codex: `timestamp` フィールド（ISO 8601）で比較
- Windsurf/Antigravity: ファイルの更新日時で比較（正確なタイムスタンプが取れない場合）

### 存在チェックの順序
1. まず各ツールのベースディレクトリが存在するかをGlobまたはlsで確認
2. 存在するツールのみデータ収集を実行
3. 検出されなかったツールはレポートの「データソースサマリー」に「未検出」と記載
