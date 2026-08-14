# Reachy Mini Wireless deployment

## 1. Copy the app to the robot

From the development computer:

```bash
scp -r reachy-openai-realtime pollen@reachy-mini.local:/tmp/
```

## 2. Install into the Wireless app environment

```bash
ssh pollen@reachy-mini.local
/venvs/apps_venv/bin/pip install /tmp/reachy-openai-realtime
```

Do not place the API key in source files, Git, a Hugging Face Space, or shell history.

## 3. Configure from the dashboard

Start `reachy_openai_realtime` from the Reachy Mini dashboard and open its settings page.
Enter the OpenAI API key there; this is the recommended setup method.

The key is stored in the robot's persistent user configuration directory with mode `0600` and cannot be read back through the UI or API. The app migrates a saved key from the former `reachy_japanese_realtime` app automatically.

Select the target conversation language in the UI. English is the default and language changes apply to the next response without restarting the app.

For temporary development only, the app can also read a process environment variable:

```bash
export OPENAI_API_KEY='YOUR_KEY'
cd /tmp/reachy-openai-realtime
/venvs/apps_venv/bin/python -m reachy_openai_realtime.main
```

## 4. Hardware checklist

1. Reachy says “Hello. Talk to me.” after connecting with the default settings.
2. Changing the UI language changes the next spoken response.
3. The microphone meter rises while a person speaks.
4. After 800 ms of silence, the input is committed and Reachy responds.
5. Speaking during Reachy's response stops queued playback.
6. Motion requests such as “nod”, “look right”, or “act surprised” produce gentle bounded motions.
7. Listening produces one small nod, not a continuous motor loop.
8. Stopping the app releases recording, playback, and the motion worker safely.

## 5. Camera

Camera input is OFF at every startup. When enabled, one still image is sent to OpenAI at the start of each detected user turn. The activity log confirms both the send attempt and Realtime API acceptance.
