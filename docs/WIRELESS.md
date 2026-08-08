# Reachy Mini Wireless deployment

## 1. Copy the app to the robot

From the development Mac:

```bash
scp -r /Users/sy/manmaruai/reachy_japanese_realtime pollen@reachy-mini.local:/tmp/
```

## 2. Install into the Wireless app environment

```bash
ssh pollen@reachy-mini.local
/venvs/apps_venv/bin/pip install /tmp/reachy_japanese_realtime
```

Do not place the API key in source files, Git, or a Hugging Face Space.

## 3. First hardware test

Run the app from an SSH shell where the API key is present:

```bash
export OPENAI_API_KEY='YOUR_KEY'
cd /tmp/reachy_japanese_realtime
/venvs/apps_venv/bin/python -m reachy_japanese_realtime.main
```

The Reachy Mini daemon must already be running. Speak Japanese after the log reports that the Realtime session is connected.

## 4. What to verify

1. Japanese input is transcribed correctly in the logs.
2. Japanese audio starts without long silence.
3. Speaking during Reachy's response stops queued playback.
4. Requests such as「うなずいて」「右を見て」「驚いて」produce gentle motions.
5. `Ctrl+C` stops recording/playback and returns the head and antennas to neutral.

## 5. Dashboard-managed startup

After the first SSH test succeeds, configure `OPENAI_API_KEY` in the environment used by the daemon/app service, restart that service, and start the installed `reachy_japanese_realtime` app from the Reachy Mini dashboard.
The exact environment configuration depends on the Wireless image/service version; do not store the key in the published app package.
