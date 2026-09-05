# ha-utility-estimator

Backfill Home Assistant utility statistics from meter readings and billing data.

大阪ガスの請求CSVと手動メーター観測から、過去の使用量を1時間単位で線形補間する外部CLIです。HACS integrationではありません。現時点ではgasのみ対応し、水道対応は設計段階です。

## 実行

Python 3.10以上（`zoneinfo`用のタイムゾーンデータが必要）。プレビューとテストには外部Pythonパッケージは不要です。DB書き込みには `python3 -m pip install -r requirements.txt` を実行してください。

```sh
# 最初に基準点から連続する請求履歴全体を読み込む
python3 gas_usage_interpolator.py gas/billing.csv

# 観測は正確な時刻で保存。まずDBには書かずプレビューを確認
python3 gas_usage_interpolator.py 459.439 --at 2026-09-04T13:53:57

# 同じ入力でDBへ反映（既存の観測と統計は重複しない）
python3 gas_usage_interpolator.py 459.439 --at 2026-09-04T13:53:57 --commit

# 新しいCSVが来たら、基準点からの履歴全体を再投入
python3 gas_usage_interpolator.py gas/billing.csv --commit
```

接続には `DATABASE_URL` または `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` を使います。未指定時は `127.0.0.1:5432`、DB名・ユーザー名 `homeassistant`。認証情報はリポジトリに保存しないでください。

CLI設定:

| 引数 | 既定値 |
| --- | --- |
| `--anchor-time` | `2026-06-25T12:00`（JST） |
| `--anchor-value` | `412.0` m³ |
| `--observations-file` | `gas_manual_observations.csv` |
| `--output` | `gas_hourly_preview.csv` |
| `--statistic-id` | `gas_estimator:usage` |

既定の基準点はv4との互換用です。別のメーターには必ず両anchor引数と別のファイル・statistic IDを指定してください。同じ系列では、CSVモードと手動モードの基準点を固定してください。

`--commit` がなくても観測ファイルとプレビューは更新されます。DBは変更されません。観測・補間の検証失敗時は新規観測を保存しません。DB失敗時にはローカルファイルが残るため、同じ引数で再実行できます。ファイルとDBをまたぐtransactionではありません。同時に複数のCLIを実行しないでください。

コードから移した関数ごとの説明は[実装メモ](docs/implementation.md)を参照してください。

## v4から維持した補間仕様

- CSVの先頭説明行を飛ばし、`ご請求月`、`ガス使用期間`、`ガス使用量(m3)`、任意の`ガス使用日数(日)`を読みます。UTF-8 BOM、UTF-8、CP932、Shift JISに対応します。
- 実装の期間パーサーは `2026年7月25日～2026年8月25日` 形式です。期間表示を日付で表すと7/25～8/25であり、内部では `[2026-07-25 12:00 JST, 2026-08-26 12:00 JST)` です。スラッシュ区切りのCSV文字列は現時点では未対応です。
- `period='-'` 等の解釈できない期間、使用量が空または`-`の行はスキップして表示します。CSV日数が期間と不一致の場合はエラーです。
- 基準点以降の請求期間は連続している必要があります。欠落・重複・重なりはエラー、基準点以前で終わる期間は無視します。対象期間が空なら終了コード2です。
- CSV使用量を順次積み上げて確定境界値を作り、期間内の手動観測を中間アンカーにします。CSV境界で値が一致しない場合や、中間値がCSV総量・単調増加に矛盾する場合はエラーです。
- 最新CSV終端後は手動観測間を線形補間します。非正時の観測も秒・マイクロ秒まで内部アンカーとして使用し、出力は正時だけ。正時の終端は含め、期間の共有境界は1行にします。
- 手動モードは既存プレビューの最新CSV終端から末尾を再構築します。CSV終端以前の観測を新規登録する用途には対応しません。後日のCSV再投入では保存済みの観測を期間内アンカーとして使用します。
- `state = メーター絶対値`、`sum = state - anchor_value`。未来の観測・統計、非有限値、メーター減少を拒否します。
- 手動観測CSVは `timestamp_jst,meter_value_m3,source`。v4同様、小数点以下6桁で保存します。同一時刻・同一値の再登録は何もしません。

提示された基準データ（412 → 437 → 453、9/4 13:53:57に459.439）では1706点です。最終正時9/4 13:00の値はv4の式で **state=459.412429、sum=47.412429** となります。引き継ぎ資料の459.417323とは差があり、観測時刻など元の実行条件の確認が必要です。補間式は変更していません。

## External statisticsとDBの注意点

通常sensor/helper、REST API、`HA_TOKEN`は不要です。旧 `--ha-url`、`--ha-token`、`--helper-entity`、`--skip-ha-update` 引数は削除しました。sensor形式のIDと `recorder:` sourceを拒否します。

新規metadataは次の属性です。sourceはstatistic IDのコロンより前から導出します。

```text
statistic_id = gas_estimator:usage
source = gas_estimator
unit_of_measurement = m³
has_mean = false
has_sum = true
name = Gas Usage Estimated
mean_type = 0
unit_class = volume
```

`statistics_meta`の作成・属性確認、`statistics`へのUPSERT、全行の時刻・state・sumの照合を同一transactionで行います。エラー時はROLLBACKします。既存metadataの属性が異なる場合は自動修正しません（nameの変更は許容）。統計のUNIQUE制約 `(metadata_id, start_ts)` を使用し、同じ入力の再投入ではstate・sumが同じ値になります。`created_ts`は再投入時に更新されます。`statistics_short_term`は作成・更新・削除しません。

**Home Assistant内部DBへの直接書き込みはバージョン依存です。** 対象は引き継ぎ時に提示されたPostgreSQL schema（metadataの`has_mean`、`mean_type`、`unit_class`、統計の`created_ts`、`start_ts`等が存在）です。HAアップグレード時にはschemaを再確認してください。実DBの接続先・実行環境は未提供のため、本リポジトリのテストだけでHAの実DB互換性やEnergy Dashboard表示は保証できません。事前にバックアップを取得し、HA停止中に書き込み、起動後に表示を確認する運用を推奨します。直接SQLはHAのキャッシュ・通知経路を通りません。

公式資料: [statisticsのデータモデル](https://data.home-assistant.io/docs/statistics/)、[mean_type / unit_classのAPI変更](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/)。HA側Python APIの`async_add_external_statistics`は将来の出力先候補ですが、そのまま外部CLIから呼べるREST APIではありません。

### 旧sensor系列からの切り替え

1. 旧helperを更新する自動化・旧CLI定期実行を停止する。
2. 基準点からの全CSVと観測でプレビューを作成・確認し、`--statistic-id gas_estimator:usage --commit` で新系列へ投入する。手動モードだけの初回投入では過去のCSV分が入りません。
3. HA起動後、Energy Dashboardのガス統計を新しいexternal IDへ切り替え、旧系列と二重選択しない。
4. 不要になった旧sensor/helperはHA設定側で整理する。

旧 `sensor.gas_usage_estimated`（metadata_id=192）の統計には触れません。移行は元の請求・観測から再生成します。旧データを自動削除・改名・コピーする機能はありません。

現行UPSERTは入力範囲だけを更新します。基準点の変更、観測の削除による期間短縮、既存範囲の一部だけの訂正では、範囲外の既存行を調整しません。通常は同じ基準点と全履歴で再計算し、基準点を変える場合は新しいstatistic IDを使ってください。

## テスト

```sh
python3 -m unittest discover -s tests -v
# pytestをインストール済みの場合も実行可能
python3 -m pytest
```

補間回帰、CSV境界・エンコーディング・スキップ行、空期間、重複時刻、観測矛盾、未来・非有限値、観測再登録、失敗時のファイル維持、終了コード、SQL再投入とtransaction制御を検証します。DBテストは接続をモックしており、実PostgreSQLの統合テストは未実施です。

## 次の段階: gas / water共通化案

今回の変更ではv4の単一ファイルとCLIを維持しています。以下をそれぞれテスト付きの小さな変更として進めます。

1. `providers/osakagas.py`へCSVのデコードとパースを抽出。大阪ガス固有の「終了日の翌日12時」規則はproviderに残す。
2. `MeterPoint`と開始・終了の正確な時刻および使用量を持つ汎用期間型、補間・制約検証を共通モジュールへ移す。単位名を含む既存CSV形式は互換読み込みを維持する。
3. `writers/postgres.py`へDB処理を抽出し、`write(series, metadata, baseline)`の境界を作る。将来HAの正式な取り込み経路を使うwriterを差し替え可能にする。
4. utility別のanchor、timezone、単位、statistic ID、保存先を設定ファイルへ移す。CLIで上書き可能にし、基準点をプレビューに記録・照合する。
5. `providers/manual_billing.py`で水道検針票を入力する。例の935→947、使用量12を検証し、検針日時間の区間に変換する。水道の5/20～7/3にはガスの翌日12時ルールを流用せず、検針時刻または設定された時刻を明示する。
6. `utility_interpolator.py gas ...` / `water ...`を追加し、既存ガスCLIは互換入口として残す。

実DB統合テスト、設定変更・範囲短縮の安全な取り扱い、ローカルファイルの原子的置換と同時実行ロックも次段階の対象です。
