# Memory E2E (hardware, spec §13 definition of done)

1. Deploy the branch to a robot (daytime robot 192.168.23.184 preferred) and start the app.
2. In conversation, tell the robot a distinctive fact ("my kazoo is named Gerald").
3. Watch events.jsonl for memory.created (IDs only — no text should appear).
4. Restart the app (dashboard restart button).
5. Ask "what do you remember about my kazoo?" — the first recall (or, once a nap has
   run, the wake block) must reflect the fact.
6. Confirm application.log and events.jsonl contain no memory text.

## Dashboard (PR 2)

On the robot (or via tailscale to it):

    curl -s http://<robot>:8042/api/memory | jq .
    curl -s -X POST http://<robot>:8042/api/memory/<mem_id>/pin \
      -H 'Content-Type: application/json' -d '{"pinned": true}' | jq .
    curl -s -X DELETE http://<robot>:8042/api/memory/<mem_id> | jq .

Then in the browser dashboard: panel lists notes newest-first with kind and
source, search filters, pin toggles the star, delete removes the row, and the
count updates. Verify in all-languages dropdown that the panel headings translate.
