"""
Hand Cricket - Multiplayer Game Server
---------------------------------------
Authoritative WebSocket server. Handles room codes, team joining (Team 1 /
Team 2, up to 10 players + 1 leader each), a bot opponent fallback,
toss, overs setup, and ball-by-ball resolution.

Run:
    pip install websockets
    python3 server.py
Server listens on ws://0.0.0.0:8765

RULE SET (placeholder / adjust freely in resolve_ball()):
  - Batter and bowler each secretly pick from {1,2,3,4,5,6,"stroke"}.
  - Same pick (numeric) on both sides           -> OUT
  - One side (or both) picks "stroke", no match -> DOT ball (no run, no wicket)
  - Different numeric picks                     -> runs scored = batter's number
  - Bowler fails to pick within 15s              -> WIDE (+1 run, ball replayed)
  - Batter fails to pick within 15s              -> OUT (missed the ball)

Two teams are fixed identities ("Team 1" / "Team 2") formed at
join time. The toss decides who actually bats / bowls first each match; roles
swap for the second innings regardless of squad name.
"""

import asyncio
import json
import random
import string
import uuid

import websockets

DEFAULT_BALL_SECONDS = 15
MIN_BALL_SECONDS = 5
MAX_BALL_SECONDS = 15
DIFFICULTIES = {"easy", "medium", "hard"}
NUMERIC = ["1", "2", "3", "4", "5", "6"]
ALL_PICKS = NUMERIC + ["stroke"]
MAX_PER_TEAM = 10

ROOMS = {}  # code -> Room


def gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in ROOMS:
            return code


class Player:
    def __init__(self, pid, name, ws=None, is_bot=False):
        self.id = pid
        self.name = name
        self.ws = ws
        self.is_bot = is_bot
        self.team = None      # "batting" | "bowling"  (squad identity)
        self.is_out = False

    def public(self):
        return {"id": self.id, "name": self.name, "team": self.team,
                "is_bot": self.is_bot, "is_out": self.is_out}


class Squad:
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.leader = None   # pid
        self.order = []      # join order, list of pid


class Room:
    def __init__(self, code, host_pid):
        self.code = code
        self.host = host_pid
        self.players = {}   # pid -> Player
        self.squads = {
            "batting": Squad("batting", "Team 1"),
            "bowling": Squad("bowling", "Team 2"),
        }
        self.overs = None
        self.phase = "lobby"   # lobby, toss, decision, innings, break, gameover
        self.toss = {}
        self.bat_team = None    # squad key actually batting this innings
        self.bowl_team = None
        self.innings_no = 0
        self.innings = {}       # runs, wickets, balls, bat_idx, bowl_idx
        self.first_innings_score = None
        self.ball = {"stage": "idle", "batter": None, "bowler": None,
                     "picks": {}, "timer_task": None, "seconds": DEFAULT_BALL_SECONDS}
        self.difficulty = "medium"
        self.ball_seconds = DEFAULT_BALL_SECONDS
        self.bot_mode = False
        self.paused = False
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.lock = asyncio.Lock()

    def squad_of(self, pid):
        p = self.players.get(pid)
        return p.team if p else None

    def all_players(self):
        return list(self.players.values())

    def state_payload(self):
        return {
            "type": "room_state",
            "code": self.code,
            "host": self.host,
            "phase": self.phase,
            "overs": self.overs,
            "difficulty": self.difficulty,
            "ball_seconds": self.ball_seconds,
            "players": [p.public() for p in self.all_players()],
            "squads": {
                k: {"label": s.label, "leader": s.leader, "order": s.order}
                for k, s in self.squads.items()
            },
            "bat_team": self.bat_team,
            "bowl_team": self.bowl_team,
            "paused": self.paused,
            "toss": self.toss,
        }

    def score_payload(self):
        i = self.innings
        return {
            "type": "score_update",
            "innings_no": self.innings_no,
            "bat_team": self.bat_team,
            "bowl_team": self.bowl_team,
            "runs": i.get("runs", 0),
            "wickets": i.get("wickets", 0),
            "balls": i.get("balls", 0),
            "overs": self.overs,
            "target": self.first_innings_score if self.innings_no == 2 else None,
            "current_batter": self.current_batter_pid(),
            "current_bowler": self.current_bowler_pid(),
        }

    def current_batter_pid(self):
        sq = self.squads[self.bat_team] if self.bat_team else None
        i = self.innings
        if not sq or "bat_idx" not in i:
            return None
        order = [pid for pid in sq.order if not self.players[pid].is_out]
        idx = i["bat_idx"]
        if idx < len(order):
            return order[idx]
        return None

    def current_bowler_pid(self):
        sq = self.squads[self.bowl_team] if self.bowl_team else None
        i = self.innings
        if not sq or "bowl_idx" not in i or not sq.order:
            return None
        return sq.order[i["bowl_idx"] % len(sq.order)]


async def send(ws, payload):
    if ws is None:
        return
    try:
        await ws.send(json.dumps(payload))
    except Exception:
        pass


async def broadcast(room, payload):
    for p in room.all_players():
        if p.ws is not None:
            await send(p.ws, payload)


async def broadcast_room_state(room):
    await broadcast(room, room.state_payload())


# ---------------------------------------------------------------- lobby ----

async def handle_create_room(ws, msg, conn_ctx):
    pid = str(uuid.uuid4())[:8]
    name = (msg.get("name") or "Player").strip()[:18]
    code = gen_code()
    room = Room(code, host_pid=pid)
    room.bot_mode = True
    player = Player(pid, name, ws)
    room.players[pid] = player
    ROOMS[code] = room
    conn_ctx["room"] = code
    conn_ctx["pid"] = pid
    await send(ws, {"type": "joined", "pid": pid, "code": code, "is_host": True})
    await broadcast_room_state(room)


async def handle_join_room(ws, msg, conn_ctx):
    code = (msg.get("code") or "").strip().upper()
    name = (msg.get("name") or "Player").strip()[:18]
    room = ROOMS.get(code)
    if not room:
        await send(ws, {"type": "error", "message": "Room not found."})
        return
    if room.phase != "lobby":
        await send(ws, {"type": "error", "message": "Match already in progress."})
        return
    if len(room.players) >= MAX_PER_TEAM * 2:
        await send(ws, {"type": "error", "message": "Room is full."})
        return
    pid = str(uuid.uuid4())[:8]
    player = Player(pid, name, ws)
    room.players[pid] = player
    conn_ctx["room"] = code
    conn_ctx["pid"] = pid
    await send(ws, {"type": "joined", "pid": pid, "code": code, "is_host": False})
    await broadcast_room_state(room)


async def handle_play_bot(ws, msg, conn_ctx):
    pid = str(uuid.uuid4())[:8]
    name = (msg.get("name") or "Player").strip()[:18]
    code = gen_code()
    room = Room(code, host_pid=pid)
    human = Player(pid, name, ws)
    room.players[pid] = human
    human.team = "batting"
    room.squads["batting"].order.append(pid)
    room.squads["batting"].leader = pid

    bot_id = "bot-" + str(uuid.uuid4())[:6]
    bot = Player(bot_id, "Bot Kabir", ws=None, is_bot=True)
    bot.team = "bowling"
    room.players[bot_id] = bot
    room.squads["bowling"].order.append(bot_id)
    room.squads["bowling"].leader = bot_id

    ROOMS[code] = room
    conn_ctx["room"] = code
    conn_ctx["pid"] = pid
    await send(ws, {"type": "joined", "pid": pid, "code": code, "is_host": True, "bot_mode": True})
    await broadcast_room_state(room)


async def handle_join_team(room, pid, msg):
    team = msg.get("team")
    if team not in ("batting", "bowling"):
        return
    player = room.players.get(pid)
    if not player or room.phase != "lobby":
        return
    if player.team == team:
        return
    # leave old squad
    if player.team:
        old = room.squads[player.team]
        if pid in old.order:
            old.order.remove(pid)
        if old.leader == pid:
            old.leader = old.order[0] if old.order else None
    squad = room.squads[team]
    if len(squad.order) >= MAX_PER_TEAM:
        await send(player.ws, {"type": "error", "message": "That squad is full."})
        return
    player.team = team
    squad.order.append(pid)
    if squad.leader is None:
        squad.leader = pid
    await broadcast_room_state(room)


async def handle_claim_leader(room, pid, msg):
    team = msg.get("team")
    squad = room.squads.get(team)
    player = room.players.get(pid)
    if not squad or not player or player.team != team or room.phase != "lobby":
        return
    squad.leader = pid
    await broadcast_room_state(room)


async def handle_set_overs(room, pid, msg):
    if pid != room.host or room.phase != "lobby":
        return
    try:
        overs = int(msg.get("overs"))
    except (TypeError, ValueError):
        return
    room.overs = max(1, min(overs, 20))
    await broadcast_room_state(room)


async def handle_set_name(room, pid, msg):
    if room.phase != "lobby":
        return
    player = room.players.get(pid)
    if not player or player.is_bot:
        return
    name = (msg.get("name") or "").strip()[:18]
    if not name:
        return
    player.name = name
    await broadcast_room_state(room)


async def handle_set_settings(room, pid, msg):
    if pid != room.host or room.phase != "lobby":
        return
    difficulty = msg.get("difficulty")
    try:
        ball_seconds = int(msg.get("ball_seconds"))
    except (TypeError, ValueError):
        return
    if difficulty not in DIFFICULTIES:
        return
    player = room.players.get(pid)
    name = (msg.get("name") or "").strip()[:18]
    if player and not player.is_bot and name:
        player.name = name
    try:
        requested_overs = int(msg.get("overs", room.overs or 1))
    except (TypeError, ValueError):
        requested_overs = room.overs or 1
    room.overs = max(1, min(requested_overs, 20))
    room.difficulty = difficulty
    room.ball_seconds = max(MIN_BALL_SECONDS, min(ball_seconds, MAX_BALL_SECONDS))
    await broadcast_room_state(room)


async def handle_start_match(room, pid, msg):
    if pid != room.host or room.phase != "lobby":
        return
    player = room.players.get(pid)
    name = (msg.get("name") or "").strip()[:18]
    if player and not player.is_bot and name:
        player.name = name
    difficulty = msg.get("difficulty", room.difficulty)
    if difficulty in DIFFICULTIES:
        room.difficulty = difficulty
    try:
        requested_seconds = int(msg.get("ball_seconds", room.ball_seconds))
    except (TypeError, ValueError):
        requested_seconds = room.ball_seconds
    room.ball_seconds = max(MIN_BALL_SECONDS, min(requested_seconds, MAX_BALL_SECONDS))
    try:
        requested_overs = int(msg.get("overs", room.overs or 1))
    except (TypeError, ValueError):
        requested_overs = room.overs or 1
    room.overs = max(1, min(requested_overs, 20))
    bat_sq, bowl_sq = room.squads["batting"], room.squads["bowling"]
    if not bat_sq.order or not bowl_sq.order:
        await send(room.players[pid].ws, {"type": "error", "message": "Both squads need at least 1 player."})
        return
    if not room.overs:
        await send(room.players[pid].ws, {"type": "error", "message": "Set the number of overs first."})
        return
    if room.bot_mode:
        await start_innings(room, decision="bat", team="batting")
        return
    room.phase = "toss"
    room.toss = {"caller_team": "batting", "caller_pid": bat_sq.leader, "call": None, "result": None, "winner_team": None}
    await broadcast_room_state(room)
    await broadcast(room, {"type": "toss_prompt", "caller_pid": bat_sq.leader, "caller_team": "batting"})
    caller = room.players[bat_sq.leader]
    if caller.is_bot:
        asyncio.create_task(bot_toss_call(room))


async def bot_toss_call(room):
    await asyncio.sleep(1.2)
    await resolve_toss_call(room, random.choice(["heads", "tails"]))


async def resolve_toss_call(room, call):
    actual = random.choice(["heads", "tails"])
    caller_team = room.toss["caller_team"]
    other_team = "bowling" if caller_team == "batting" else "batting"
    winner_team = caller_team if call == actual else other_team
    room.toss.update({"call": call, "result": actual, "winner_team": winner_team})
    room.phase = "decision"
    await broadcast(room, {"type": "toss_result", **room.toss})
    await broadcast_room_state(room)
    winner_leader_pid = room.squads[winner_team].leader
    await broadcast(room, {"type": "decision_prompt", "team": winner_team, "leader_pid": winner_leader_pid})
    winner_leader = room.players.get(winner_leader_pid)
    if winner_leader and winner_leader.is_bot:
        asyncio.create_task(bot_toss_decision(room, winner_team))


async def bot_toss_decision(room, team):
    await asyncio.sleep(1.2)
    await start_innings(room, decision="bat" if random.random() < 0.5 else "bowl", team=team)


async def start_innings(room, decision, team):
    if decision == "bat":
        room.bat_team, room.bowl_team = team, ("bowling" if team == "batting" else "batting")
    else:
        room.bowl_team, room.bat_team = team, ("bowling" if team == "batting" else "batting")
    room.innings_no = 1
    room.innings = {"runs": 0, "wickets": 0, "balls": 0, "bat_idx": 0, "bowl_idx": 0}
    room.phase = "innings"
    room.paused = False
    room.pause_event.set()
    await broadcast_room_state(room)
    await broadcast(room, room.score_payload())
    await broadcast(room, {"type": "innings_start", "innings_no": 1,
                           "bat_team": room.bat_team, "bowl_team": room.bowl_team,
                           "difficulty": room.difficulty, "ball_seconds": room.ball_seconds})
    await prompt_next_ball(room)


# ------------------------------------------------------------ ball flow ----

async def prompt_next_ball(room):
    bowler = room.current_bowler_pid()
    batter = room.current_batter_pid()
    room.ball = {"stage": "await_shake", "batter": batter, "bowler": bowler,
                 "picks": {}, "timer_task": None, "seconds": room.ball_seconds}
    await broadcast(room, {"type": "await_shake", "batter": batter, "bowler": bowler})
    bowler_player = room.players.get(bowler)
    if bowler_player and bowler_player.is_bot:
        asyncio.create_task(bot_shake(room))


async def bot_shake(room):
    await asyncio.sleep(random.uniform(1.0, 2.0))
    await room.pause_event.wait()
    if room.ball["stage"] == "await_shake":
        await handle_shake(room, room.ball["bowler"])


async def handle_shake(room, pid):
    if room.ball["stage"] != "await_shake" or pid != room.ball["bowler"]:
        return
    room.ball["stage"] = "picking"
    room.ball["picks"] = {}
    await broadcast(room, {"type": "ball_start", "seconds": room.ball_seconds})

    batter = room.players.get(room.ball["batter"])
    bowler = room.players.get(room.ball["bowler"])
    if batter and batter.is_bot:
        asyncio.create_task(bot_pick(room, batter.id))
    if bowler and bowler.is_bot:
        asyncio.create_task(bot_pick(room, bowler.id))

    room.ball["timer_task"] = asyncio.create_task(ball_timer(room))


async def bot_pick(room, pid):
    delay_ranges = {
        "easy": (3.0, max(3.1, room.ball_seconds - 1.0)),
        "medium": (1.5, max(1.6, min(8.0, room.ball_seconds - 1.0))),
        "hard": (0.5, max(0.6, min(3.0, room.ball_seconds - 1.0))),
    }
    delay_range = delay_ranges[room.difficulty]
    await asyncio.sleep(random.uniform(*delay_range))
    await room.pause_event.wait()
    if room.ball["stage"] == "picking" and pid not in room.ball["picks"]:
        opponent_role = "bowler" if pid == room.ball["batter"] else "batter"
        opponent_pick = room.ball["picks"].get(opponent_role)
        value = random.choice(ALL_PICKS)
        if room.difficulty == "hard" and opponent_pick in NUMERIC:
            value = opponent_pick if pid == room.ball["bowler"] else random.choice([n for n in NUMERIC if n != opponent_pick])
        await handle_pick(room, pid, value, internal=True)


async def ball_timer(room):
    for remaining in range(room.ball_seconds, -1, -1):
        await room.pause_event.wait()
        if room.ball["stage"] != "picking":
            return
        await broadcast(room, {"type": "timer", "seconds": remaining})
        await asyncio.sleep(1)
    if room.ball["stage"] == "picking":
        await resolve_ball(room)


async def handle_pick(room, pid, value, internal=False):
    if room.ball["stage"] != "picking":
        return
    player = room.players.get(pid)
    if not player or (player.is_bot and not internal):
        return
    if pid not in (room.ball["batter"], room.ball["bowler"]):
        return
    if value not in ALL_PICKS:
        return
    role = "batter" if pid == room.ball["batter"] else "bowler"
    room.ball["picks"][role] = value
    await broadcast(room, {"type": "pick_locked", "role": role})
    if "batter" in room.ball["picks"] and "bowler" in room.ball["picks"]:
        if room.ball["timer_task"]:
            room.ball["timer_task"].cancel()
        await resolve_ball(room)


async def toggle_pause(room, pid):
    if pid != room.host or room.phase != "innings":
        return
    room.paused = not room.paused
    if room.paused:
        room.pause_event.clear()
    else:
        room.pause_event.set()
    await broadcast(room, {"type": "game_paused", "paused": room.paused})


async def return_to_lobby(room, pid):
    if pid not in room.players or room.phase not in {"innings", "break", "gameover"}:
        return
    if room.ball.get("timer_task"):
        room.ball["timer_task"].cancel()
    room.phase = "lobby"
    room.paused = False
    room.pause_event.set()
    room.toss = {}
    room.bat_team = None
    room.bowl_team = None
    room.innings_no = 0
    room.innings = {}
    room.first_innings_score = None
    room.ball = {"stage": "idle", "batter": None, "bowler": None,
                 "picks": {}, "timer_task": None, "seconds": room.ball_seconds}
    for player in room.all_players():
        player.is_out = False
    await broadcast_room_state(room)


async def resolve_ball(room):
    if room.ball["stage"] != "picking":
        return
    room.ball["stage"] = "resolving"
    picks = room.ball["picks"]
    batter_pick = picks.get("batter")
    bowler_pick = picks.get("bowler")
    i = room.innings
    outcome = {}

    if bowler_pick is None:
        i["runs"] += 1
        outcome = {"result": "wide", "runs": 1, "wicket": False, "ball_counts": False}
    elif batter_pick is None:
        i["wickets"] += 1
        outcome = {"result": "out", "runs": 0, "wicket": True, "ball_counts": True}
        _mark_out(room)
    elif batter_pick == bowler_pick:
        i["wickets"] += 1
        outcome = {"result": "out", "runs": 0, "wicket": True, "ball_counts": True}
        _mark_out(room)
    elif batter_pick == "stroke" or bowler_pick == "stroke":
        outcome = {"result": "dot", "runs": 0, "wicket": False, "ball_counts": True}
    else:
        runs = int(batter_pick)
        i["runs"] += runs
        outcome = {"result": "runs", "runs": runs, "wicket": False, "ball_counts": True}

    if outcome["ball_counts"]:
        i["balls"] += 1

    await broadcast(room, {
        "type": "ball_result",
        "batter_pick": batter_pick,
        "bowler_pick": bowler_pick,
        **outcome,
    })
    await broadcast(room, room.score_payload())

    # over completed -> rotate bowler
    if outcome["ball_counts"] and i["balls"] % 6 == 0 and i["balls"] > 0:
        i["bowl_idx"] = (i["bowl_idx"] + 1) % max(len(room.squads[room.bowl_team].order), 1)

    ended = await check_innings_end(room)
    if ended:
        return

    room.ball["stage"] = "idle"
    await asyncio.sleep(2.0)
    await prompt_next_ball(room)


def _mark_out(room):
    i = room.innings
    batter_pid = room.current_batter_pid()
    if batter_pid:
        room.players[batter_pid].is_out = True
    i["bat_idx"] += 1


async def check_innings_end(room):
    i = room.innings
    bat_squad_size = len(room.squads[room.bat_team].order)
    all_out = i["wickets"] >= bat_squad_size
    overs_done = i["balls"] >= room.overs * 6
    target_reached = room.innings_no == 2 and room.first_innings_score is not None and i["runs"] > room.first_innings_score

    if target_reached:
        await end_match(room, chase_success=True)
        return True

    if all_out or overs_done:
        if room.innings_no == 1:
            room.first_innings_score = i["runs"]
            # swap roles
            room.bat_team, room.bowl_team = room.bowl_team, room.bat_team
            room.innings_no = 2
            room.innings = {"runs": 0, "wickets": 0, "balls": 0, "bat_idx": 0, "bowl_idx": 0}
            for p in room.all_players():
                p.is_out = False
            room.phase = "innings"
            await broadcast_room_state(room)
            await broadcast(room, {
                "type": "innings_break",
                "first_innings_score": room.first_innings_score,
                "next_bat_team": room.bat_team,
                "next_bowl_team": room.bowl_team,
                "next_batter": room.current_batter_pid(),
                "next_bowler": room.current_bowler_pid(),
            })
            await broadcast(room, room.score_payload())
            await asyncio.sleep(3.0)
            await prompt_next_ball(room)
            return True
        else:
            await end_match(room, chase_success=False)
            return True
    return False


async def end_match(room, chase_success):
    room.phase = "gameover"
    target = room.first_innings_score
    chasing_runs = room.innings["runs"]
    if chase_success or chasing_runs > target:
        winner = room.bat_team
        margin = f"won by {max(len(room.squads[room.bat_team].order) - room.innings['wickets'], 0)} wicket(s)"
    elif chasing_runs == target:
        winner = None
        margin = "Match tied!"
    else:
        winner = room.bowl_team
        margin = f"won by {target - chasing_runs} run(s)"
    await broadcast(room, {
        "type": "game_over",
        "winner_team": winner,
        "summary": margin,
        "first_innings_score": target,
        "second_innings_score": chasing_runs,
    })


# --------------------------------------------------------------- server ----

async def handler(ws):
    conn_ctx = {"room": None, "pid": None}
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            if mtype == "create_room":
                await handle_create_room(ws, msg, conn_ctx)
            elif mtype == "join_room":
                await handle_join_room(ws, msg, conn_ctx)
            elif mtype == "play_bot":
                await handle_play_bot(ws, msg, conn_ctx)
            else:
                room = ROOMS.get(conn_ctx["room"])
                pid = conn_ctx["pid"]
                if not room or not pid:
                    continue
                async with room.lock:
                    if mtype == "join_team":
                        await handle_join_team(room, pid, msg)
                    elif mtype == "claim_leader":
                        await handle_claim_leader(room, pid, msg)
                    elif mtype == "set_overs":
                        await handle_set_overs(room, pid, msg)
                    elif mtype == "set_name":
                        await handle_set_name(room, pid, msg)
                    elif mtype == "set_settings":
                        await handle_set_settings(room, pid, msg)
                    elif mtype == "start_match":
                        await handle_start_match(room, pid, msg)
                    elif mtype == "toss_call":
                        if pid == room.toss.get("caller_pid"):
                            await resolve_toss_call(room, msg.get("call"))
                    elif mtype == "toss_decision":
                        winner_team = room.toss.get("winner_team")
                        if winner_team and pid == room.squads[winner_team].leader:
                            await start_innings(room, msg.get("decision"), winner_team)
                    elif mtype == "shake":
                        await handle_shake(room, pid)
                    elif mtype == "pick":
                        await handle_pick(room, pid, msg.get("value"))
                    elif mtype == "toggle_pause":
                        await toggle_pause(room, pid)
                    elif mtype == "return_to_lobby":
                        await return_to_lobby(room, pid)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        room = ROOMS.get(conn_ctx["room"])
        pid = conn_ctx["pid"]
        if room and pid and pid in room.players:
            room.players[pid].ws = None
            if room.phase == "lobby":
                # fully remove from lobby so seats free up
                player = room.players.pop(pid)
                if player.team:
                    sq = room.squads[player.team]
                    if pid in sq.order:
                        sq.order.remove(pid)
                    if sq.leader == pid:
                        sq.leader = sq.order[0] if sq.order else None
                if room.host == pid:
                    remaining = list(room.players.keys())
                    room.host = remaining[0] if remaining else None
                if not room.players:
                    ROOMS.pop(room.code, None)
                else:
                    await broadcast_room_state(room)


async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765, max_size=2**20):
        print("Hand Cricket server running on ws://0.0.0.0:8765")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
