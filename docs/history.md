---
source_agent: claude-code
machine: macbook-air
created: 2026-10-18
updated: 2026-10-18
status: history
---

# これまでの検証・運用の記録（旧READMEから移動）

2026-10-18にREADMEを書き直した際、開発中の検証記録・運用メモをここへ移しました。内容は当時のままです。利用者が設定するときに従う手順ではありません。

## Claude Codeでの利用確認

利用者から共有された実機検証結果では、2026-10-04にMacBook AirのClaude Codeから読み取り・作成・追記・再読み込みを確認し、2026-10-12には成果物3件の要約の書き込みと再読み込みを確認しています。Mac miniの認証未復旧とは、環境を分けて扱います。以下の過去記録と `docs/handoff.md` には更新前の状況が残っています。

## 過去の検証・運用記録（2026-09-26整理）
これはMicrosoft Graphを直接呼ぶPython CLIです。**MCPサーバーは未実装**です。
現在の状態・次の作業は [docs/handoff.md](handoff.md) を参照してください。

- 2026-09-22の実機読み書き・同期は成功済み。
- Mac miniでの最終実機確認（2026-09-23）はKeychainアクセスがOSStatus -25293で失敗。認証は未復旧で、現在の接続成功を保証しません。
- 自動同期はPAUSED。ユーザーの最新指示により、今後は必要時の手動同期だけを行います。認証復旧を理由に自動再開しません。
- ローカル設定、取得したノート、添付、デモ実行結果、認証情報はGitHubに含めません。別のMacでは設定・認証が必要です。
- GitHub: https://github.com/zenbuai-tech/ClaudeOneNoteGraph-public （公開）。
- 別に用意している読み取り専用MCPの設計資料（非公開）は、この読み書きCLIとは権限・対象制限が異なります。

2026-09-22、法人Microsoft 365の実環境で認証・一覧取得・新規ページ作成・本文取得・追記・読み戻しに成功。
設定済みの保存先は「Claude接続テスト_2026-09-14」→「架空データ_読取検証」。詳細は[検証記録](verification.md)。
Python 3.9以降。認証の保存は既定でmacOS標準のKeychain、明示した場合だけローカルファイル（WindowsではDPAPIで暗号化）。追加パッケージ不要。
Claude CodeからこのCLIを実行して使える。MCPへの登録は不要で、必要になった時に追加する。

## next_action
初期ゴール完了。次に実務で使う保存先と運用ルールを決める。書き込み先は設定した1セクションに固定する。共有SharePointのノートは対象と権限を別途確認する。

## OneNoteを正本にするObsidianミラー（2026-09-22追加）

```sh
cd /path/to/ClaudeOneNoteGraph
python3 sync.py
```

- 設定済みセクションから `demo-vault/OneNote_Mirror/` へMarkdownを保存。
- `demo-vault/AI_Outputs/` は人・AIの成果物用。同期処理では上書きしない。
- ページIDで固定名を作り、aliasesと本文見出しにOneNoteタイトルを保持。タイトル変更で重複しない。
- 更新日時で本文取得を省略。直前の取得と重なる更新は再確認し、本文が同じならファイルを書き直さない。
- 手編集・未登録ファイル・別セクションの状態ファイルを検知したら上書きしない。ファイルを自動削除しない。
- 画像・添付は同じGraphルートのresourceのみ取得。未対応・失敗はconversion_warningsとlast-run.jsonに残す。
- Markdown変換は完全なレイアウト再現ではない。元のURLを保持して照合できる。
- 1回100リクエストを既定上限とし、未処理は次回へ継続。メタデータを全件走査するため、大規模環境は頻度・予算の計測が必要。
- 状態は `demo-vault/.onenote-sync/state.json`、最新結果は `last-run.json`。プロセスロック・原子的保存あり。
- `--vault` で別の保存先を指定できる。別のOneNoteソースは別のVaultを使う。シンボリックリンクを含まない実パスを指定する。
- Keychainの読取・保存・存在確認・削除は、同じPythonプロセス内のSecurity.frameworkで対話を無効化して行う。許可不足・ロック時はパスワード画面を開かずエラーにする。`security` コマンドは起動しない。アクセス制御やMacのロック設定は変更しない。

### 同期の運用方針

過去に30分間隔の自動同期（ID: onenote-obsidian）を登録しましたが、現在はPAUSEDです。ユーザーが「30分ごとのセッティングは今のところいらない」と指定したため、必要時の手動同期だけを行います。認証復旧後も自動再開しません。

最終実機確認（2026-09-23T03:02:28Z）はexit 1 / needs_attention / requests=0 / created=[] / updated=[]でした。非対話Keychainアクセスが拒否される問題は未復旧です。今回のGitHub登録では再認証や実同期を実行していません。

### 実演と再実行

`python3 demo.py` は新規の架空ページで直接作成→読取→同期→追記→同期→無変更再実行を確認する。完了済みなら `demo-result.json` の証跡を表示し、新しいページを作らない。書き込み途中に失敗した場合は、ジャーナルと対象ページを確認してから再開する。

既存Macの見せ方・ヒアリング項目はローカルの `demo-vault/00_デモ案内.md` を参照（Git管理対象外）。新しいチェックアウトにはデモ完了記録がないため、demo.pyを実行すると新規ページが作られます。接続確認のために自動実行しないでください。

## next_action（現在）
Claude CodeでREADMEを読み、利用環境に合わせて設定・認証を行い、既存ページの読み取りから確認する。Mac miniでは `auth_store=file` の新規ログイン・保存・個人用ページの読み取りを確認済み。旧設定のセクションIDは現在のアカウントで404になったため、現在の一覧から検証用設定を別に作って確認した。SharePointサイトは既存の `Notes.ReadWrite` で403となったが、利用者の明示的な許可後に `Notes.ReadWrite.All` で新しくログインし、リンク解決とテストページ1件の作成・読み取り・追記・再読み込みまで成功（[ローカル認証の設計](local-auth.md)）。自動同期は再開しない。チーム用の `link` と読み書きは実機確認済み。個人用URLの `link` とWindowsは追加確認する（[簡易版の設計](link-target-mvp.md)）。Windowsは `init-local` → `login` → `status` → `pages` → `read` の順に実機で確かめる。
