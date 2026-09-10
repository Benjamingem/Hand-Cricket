# Hand Cricket — Classroom Showdown

A real multiplayer hand-cricket game: Python WebSocket server (authoritative
game logic) + a plain HTML/CSS/JS client (no build step, no frameworks).

## Folder structure
```
server.py           <- Python backend (rooms, room codes, bot AI, rules)
client/
  index.html
  style.css
  app.js
```

## Run it

1. Install the one dependency:
   ```
   pip install websockets
   ```
2. Start the server:
   ```
   python3 server.py
   ```
   It listens on `ws://0.0.0.0:8765`.
3. Open `client/index.html` in a browser (just double-click it, or serve the
   `client/` folder with any static file server). The connect screen defaults
   to `ws://localhost:8765` — change that field if the server is on another
   machine (e.g. `ws://192.168.1.23:8765` for a friend on your LAN, or your
   deployed server's address if hosting online for friends elsewhere).

## Playing

- **Create room** → get a 5-character room code → share it with friends.
- **Join room** → enter that code.
- **Play vs Bot** → instantly starts a 1-human-vs-1-bot match, no code needed.
- In the lobby, everyone joins either **Team 1** or **Team 2** (max 10 each).
  First person into a squad becomes its leader
  (shown with a crown/LEADER tag).
- The room host sets the number of overs and starts the match.
- **Toss**: Team 1's leader calls heads/tails. Whoever wins picks
  to bat or bowl first. Roles fully swap for the second innings, regardless
  of squad name — squads are fixed identities, but who's *actually* batting
  vs bowling each innings is decided by the toss.
- **Each ball**: the current bowler taps **SHAKE** → hands animate → a 15s
  timer starts → the active batter and bowler each secretly pick from
  `1–6` or `Stroke` → hands reveal the picks with real finger-count poses
  (1 finger, 2 fingers, … thumbs-up for 6, closed fist for Stroke).

## Current rule set (easy to change in `server.py: resolve_ball()`)

| Situation | Result |
|---|---|
| Batter & bowler pick the same number | **Out** |
| Either side picks "Stroke" (no match) | The other side's number is scored |
| Different numbers | Runs = batter's number |
| Bowler doesn't pick in time | **Wide**, +1 run, ball replayed |
| Batter doesn't pick in time | **Out** (missed it) |

Bowler rotates automatically every completed over. When a batter is out, the
next player in that squad's join order comes in. Innings ends on all-out or
overs completed; second innings ends immediately if the target is chased down.

## Known simplifications (worth knowing before you extend this)

- Batting/bowling order is simply squad join order — there's no UI yet for a
  leader to manually set the batting order or choose the next bowler.
- If a human disconnects mid-match there's no reconnect flow yet — they just
  stop responding (their picks time out as normal, which the bot's teammates
  will need to work around).
- Only the currently-active batter and bowler can act each ball; other squad
  members watch and wait their turn.
- For friends outside your LAN, you'll need to host `server.py` somewhere
  reachable (a small cloud VM, Replit, etc.) and point the client's
  "Server address" field at it.

## Suggested next steps

1. Add a lobby control for leaders to set batting order / next bowler manually.
2. Add reconnect support (currently disconnects just go stale).
3. Add sound effects and a proper coin-flip 3D animation.
4. Deploy `server.py` somewhere persistent so the room code works over the internet, not just LAN.
