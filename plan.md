# Reachy Japanese Realtime — Implementation Plan

## Goal

Reachy Mini Wireless 上で動作する、日本語品質を重視した低遅延の音声会話アプリを作る。
OpenAI Realtime API の最新モデル `gpt-realtime-2.1` を利用し、会話内容に応じた安全なロボットモーションをツール呼び出しで実行する。

## Confirmed decisions

- Hardware: Reachy Mini Wireless
- Runtime: Wireless 本体（CM4）上の Python app
- Realtime model: `gpt-realtime-2.1`
- Language: 日本語を優先し、自然な短い音声応答を生成
- Authentication: `OPENAI_API_KEY`（環境に設定済み、コードやログには出さない）
- Distribution: まずローカルのみ。Hugging Face 公開は実機検証後
- Media: カメラは使わず `gstreamer_no_video` で負荷を抑える

## Architecture

1. Reachy Mini のマイク音声をPCMストリームとして取得する。
2. WebSocketでOpenAI Realtime APIへ送信する。
3. Realtime APIから返る音声をReachy Miniのスピーカーへストリーム再生する。
4. モデルへ意味レベルのモーションツールを公開する。
5. ツール呼び出しはキューへ積み、専用ワーカーがReachy Mini SDKを操作する。
6. ユーザー割り込み時は応答音声とキャンセル可能なモーションを停止する。

## Motion tools

- `look(direction)`: front / left / right / up / down
- `nod(count)`: 1〜3回のうなずき
- `shake_head(count)`: 1〜3回の首振り
- `express(emotion)`: neutral / happy / curious / surprised / sad
- `stop_motion()`: キューと実行中の動作を停止

モデルには生の関節角度を渡させない。アプリ側で角度、速度、回数、実行時間を検証し、安全なプリセットへ変換する。

## Implementation steps

1. 設定、Realtimeイベント、モーション命令を型で定義
2. 安全なモーションキューとWireless向けSDKアダプターを実装
3. Realtime WebSocketクライアントとfunction callingを実装
4. Reachy Miniの音声入出力へ接続
5. モックを使った単体テストを追加
6. 公式app checker、静的解析、単体テストを実行
7. Wireless実機へのインストール手順を文書化

## Acceptance criteria

- 日本語の音声入力に日本語音声で応答する
- 会話中にツール呼び出しから安全な動作を実行できる
- 不正な方向、感情、回数、角度を拒否またはクランプする
- API切断、音声デバイス異常、モーション失敗でアプリ全体が固まらない
- 停止時に音声・WebSocket・モーションワーカーを終了し、安全姿勢へ戻る

## Deferred settings

- 声の種類
- 発話速度と応答の長さ
- 自発的なアイドルモーション頻度
- カメラ入力と視覚ツール
- Hugging Face Spaceへの公開範囲

