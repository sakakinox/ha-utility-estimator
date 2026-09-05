# 実装メモ

`gas_usage_interpolator.py` と `tests/test_gas.py` 内の説明コメント・docstringをこの文書に移しました。元の実装は `2026-09-04-v4` で、現在はexternal statisticsに対応しています。使い方と設定値は[README](../README.md)を参照してください。

## データと設定

`MeterPoint`は時刻・メーター絶対値・出典を保持します。`BillingPeriod`は請求月・表示期間・使用量・日数を保持します。CSVは期間の増加量、手動観測はその時刻の絶対値です。

v4の既定基準点は新居の信頼できる起点として設定された2026-06-25 12:00 JST、412.000 m³です。CLIから変更できます。統計のstateはメーター絶対値、sumはこの基準値を引いた累積使用量です。

## 大阪ガスCSV

`load_osakagas_csv`はCSVの先頭説明行を読み飛ばしてからヘッダーを読みます。

`BillingPeriod.start_ts`は開始日の12:00 JSTです。表示期間は両端の日付を含むため、`end_ts`は終了日の翌日12:00 JSTとし、内部では半開区間 `[start, end)` として扱います。

## 手動観測の保存

`save_manual_observation`は保存した場合に `True`、同一時刻・同一値がすでに存在する場合に `False` を返します。同一時刻の異なる値はエラーです。

## 正時への補間

`interpolate_between`は開始点・終了点と途中の観測点をアンカーとして、区間ごとに線形補間します。アンカーは正時でなくてもよく、出力は正時だけです。

開始点が正時なら最初にその点を追加し、ループは次の正時から処理して重複を防ぎます。CSV境界など、終了点が正時の場合はその終端も出力します。

`build_period_series`はCSVの総量を正として終端値を決めます。期間内の手動観測を途中アンカーとして再按分し、矛盾する観測は自動補正せずエラーにします。

## CSV終端以降の延長

`extend_series_with_manual_tail`には、最新のCSV確定点で終わる系列を渡します。その終端から後の手動観測までを順に補間し、結合時に正時の共有境界が重複すれば除外します。次の区間は直前の補間値ではなく、正確な実測時刻・実測値から開始します。

戻り値は補間済みの系列と最後の正確な手動観測です。後続の手動観測がない場合、第2要素は `None` です。

`rebuild_manual_tail_from_preview`はプレビュー内の最新CSV終端を探し、それ以降の古い補間値を捨て、保存済みの手動観測から毎回再計算します。後日その区間を含むCSVが来た場合はCSVモードで期間全体を再構築します。

## 検証とDB書き込み

`validate_hourly_points`は正時の点を抽出・整列し、重複、欠落、メーター減少などを検査します。JSTには夏時間がないため、隣接点の時刻差は3600秒を期待します。

`csv_mode`はCSV系列に保存済みの後続観測をつないでプレビューを作成し、`--commit`指定時に系列を書き込みます。`record_mode`は末尾を再構築し、DBへはCSV終端以降だけをUPSERTします。

DBへの変更は `--commit` 指定時だけです。書き込み先はexternal statisticsであり、通常sensor/helperやHome Assistant REST APIには依存しません。`statistics_short_term`は操作しません。transactionと移行時の注意点は[README](../README.md#external-statisticsとdbの注意点)に記載しています。

## テストの範囲

`DatabaseTests`はDB接続をモックしてSQLとtransaction制御を検証します。実PostgreSQLとの互換性確認は別途必要です。

## 水道CLI

`utility_interpolator.main`はutility名を解釈し、ガスの既存CLIまたは `water_usage_interpolator.main` に残りの引数を渡します。ガス側の `main(argv)` は引数未指定時に従来どおりコマンドラインを読みます。

水道の `parse_reading_time` は日付のみの入力を当日の12:00 JSTに変換し、日時指定は正確に保持します。`validate_bill`は非負・単調増加の指針、過去の正しい期間、指針差と請求使用量の一致を確認します。

`build_series`は初回の前回指針を基準値とし、登録順に隣接する検針期間の境界時刻・指針を照合します。ガスの `interpolate_between` と `validate_hourly_points` を再利用し、共有する正時境界の重複を除外します。大阪ガスの `BillingPeriod` は使いません。

`add_bill`は同一期間・同一指針なら既存履歴を返し、異なる指針の再登録を拒否します。新規期間は末尾に連続する場合だけ追加できます。`load_history`はJSONのversion・statistic IDと履歴全体を検証します。基準点をずらさないよう、過去への追加は許可しません。

`atomic_write`は同じディレクトリに一時ファイルを作り、書き込み完了後に置換します。水道CLIは全入力検証後に履歴・プレビューを順に保存し、`--commit`指定時に履歴全体をDBに反映します。並行実行のロックや複数ファイルの一括transactionは提供しません。

共用する `commit_statistics` と `get_metadata_id` にはmetadata表示名の引数を追加しています。既定はガスのまま、水道は `Water Usage Estimated` を渡します。水道は `water_estimator:` 名前空間と履歴内のID照合で誤投入を防ぎます。
