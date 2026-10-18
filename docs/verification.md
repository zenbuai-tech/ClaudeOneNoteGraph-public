---
source_agent: claude-code
machine: mac-mini
created: 2026-09-22
status: verified
---

# 検証記録

## ローカル
- Python 3.9.6 / macOS。外部依存のインストールなし。
- CLI起動・ヘルプ確認済み。
- オフラインテスト13件成功。対象セクションの境界、Graph以外への送信拒否、HTMLエスケープ、API形式、読み取り再試行、書き込み非再送、認証更新・ページング・PKCE/state照合・秘密値の出力防止を確認。
- 模擬応答によるテストであり、Microsoftサービスでの成功を意味しない。

## Microsoft側
- 2026-09-22: 法人Microsoft 365であることをユーザー確認。
- 対象は利用者の法人Microsoft 365の組織。ユーザーより同組織で進める指示あり。
- Entra管理画面へのサインインを確認。専用アプリのシングルテナント登録画面を準備。
- ユーザーのポリシー同意を受けて専用アプリを登録済み。アプリIDとテナントIDはCLIの公開設定に保存済み（秘密キーなし）。
- Device Code認証は530035（BlockedBySecurityDefaults）で拒否。組織のセキュリティ設定は維持。
- 通常のブラウザ認証＋PKCEを実装。モバイル/デスクトップ用の`http://localhost:8765/callback`を登録し、同意画面まで到達。
- 同意画面にはOneNote読み書き（権限定義には共有も含む）、継続アクセス、基本プロフィールが表示。ユーザーの「OneNoteへのアクセスを許可する」を受けて承諾済み。組織全体の同意チェックはオフ。
- 初回トークン保存でsecurity対話入力の文字数制限に到達。ダミー12,000文字で再現。macOS Security.frameworkの直接呼び出しに変更し、12,000/16,000文字の保存・更新・読み戻しに成功。ダミー専用項目は削除済み。
- 通常ブラウザ＋PKCEで再認証し、専用Keychainへの実トークン保存・一致確認に成功。秘密値は表示・ファイル保存していない。

## 実環境での読み書き（2026-09-22 15:28 JST）
- ノートブック一覧・セクション一覧の取得に成功。
- ノートブック: Claude接続テスト_2026-09-14。
- セクション: 架空データ_読取検証。現在のCLI設定先。
- 作成ページ: 接続テスト用の架空ページ（2026-09-22 15:28作成）。
- verifyコマンドが `verified: true` を返した。create / read / append / read-backすべて成功。一意の架空検証文字列と追記文字列の包含を実際の本文取得で確認した。
- テストページは証拠として残した。既存ページの本文変更・削除なし。
- 今回の確認は本人のノートの本文読み書き。共有SharePointサイトのノート、Claudeからの呼び出し、実際の期限切れ後のトークン更新、添付・画像は未検証。

## next_action
このCLIからのOneNote読み書きという初期ゴールは完了。実務の保存先・書き込みルールを決める。以後は既存ページIDで読み取り、create/appendで必要な内容を保存する。verifyは実行ごとに新しいテストページができるため、日常の接続確認には使わない。


## OneNote / Obsidian demo verification (2026-09-22)

- source_agent: claude-code / machine: mac-mini
- Offline tests: 27 passed (existing 13 + mirror 14).
- Live Graph create/read/append/read-back: success.
- Initial mirror: created 1, unchanged 6; Graph requests 2.
- After append: updated 1, unchanged 6; Graph requests 2.
- Repeat: created 0, updated 0, unchanged 7; no duplicate/rewrite.
- Evidence: ../demo-result.json; timestamp 2026-09-22T07:29:45+00:00
- Heartbeat registered: onenote-obsidian, every 30 minutes. Scheduled first firing not yet verified.
- UI demo blocked by locked Mac. Later Keychain access stalled; own read-only process stopped and 20-second timeout added/tested.
- Media downloads implemented but not live-verified. Shared notebooks / multi-user / 3000-page performance not yet verified.

## next_action
Unlock Mac, verify Obsidian display and background authentication, then conduct workflow interview using demo guide.

- 2026-09-22T14:29:12Z: post-timeout live check returned needs_attention after 20 seconds, requests=0, Keychain timeout. No Graph request or file replacement occurred. Unlock/Keychain access is the current operational blocker.

## 2026-09-23: repeated Keychain prompt fix

- source_agent=claude-code / machine=mac-mini.
- User screenshot identifies `security` requesting this tool's Keychain item. Scheduled reads launched that CLI every 30 minutes; its timeout did not suppress its authorization UI. The previous timeout-only mitigation was insufficient.
- Paused automation `onenote-obsidian` via app tool; response confirmed PAUSED.
- Replaced CLI reads/existence checks/deletion with in-process Security.framework calls. All native operations disable interaction before touching an item; failure to disable interaction aborts. No ACL changes, password handling, keychain unlock, or plaintext credential files.
- Offline tests: 33 passed, including prompt suppression ordering for all operations, fail-closed setup, missing-item behavior, 20KB fake data, memory release, redacted parse errors, write/readback, and sync abort before HTTP when authentication is unavailable.
- Staged native read on this Mac returned OSStatus -25293 in 0.022 seconds without launching `security` or requesting user interaction. Credential contents were not printed; no Graph requests made. Authentication remains unavailable, so scheduled sync remains paused.

- Installed-code verification at 2026-09-23T03:02:28Z: manual sync exited 1 immediately with OSStatus -25293; requests=0, created=[], updated=[]. All 33 installed tests passed. Automation remains PAUSED pending credential access recovery.

## 2026-09-26: GitHub handoff

- source_agent=claude-code / machine=mac-mini.
- Offline tests: 33 passed (`python3 -B -m unittest discover -s tests -v`). No live Graph or Keychain access performed.
- Source code, tests, README, design and handoff instructions prepared for a private repository.
- Local configuration, demo-result.json, demo-vault (including source notes, attachments and state), and credentials are excluded from Git. Historical references to these files above refer to local evidence, not files shipped in this repository.
- Current authentication remains unverified since the last failed live check. Latest user policy: manual sync only; automation stays PAUSED even after credential recovery.
- next_action: Read docs/handoff.md; choose CLI use or MCP implementation, then separately verify authentication and an existing test page when requested.
