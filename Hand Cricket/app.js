// Hand Cricket - client
// Talks to server.py over WebSocket. Server is authoritative for all game
// state; this file just renders whatever it's told and forwards user input.

let ws = null;
let myPid = null;
let isHost = false;
let roomState = null;

const $ = (sel) => document.querySelector(sel);
const $all = (sel) => Array.from(document.querySelectorAll(sel));

function showScreen(id){
  $all('.screen').forEach(s => s.classList.remove('active'));
  $(`#${id}`).classList.add('active');
}

// ---------------------------------------------------------------- connect --

$('#btnCreateRoom').addEventListener('click', () => {
  connectThen(() => send({ type: 'create_room', name: nameOrDefault() }));
});

$('#btnJoinRoom').addEventListener('click', () => {
  const code = $('#joinCodeInput').value.trim().toUpperCase();
  if (!code) { setConnectError('Enter a room code.'); return; }
  connectThen(() => send({ type: 'join_room', code, name: nameOrDefault() }));
});

$('#btnPlayBot').addEventListener('click', () => {
  connectThen(() => send({ type: 'play_bot', name: nameOrDefault() }));
});

function nameOrDefault(){
  return $('#nameInput').value.trim() || 'Player';
}

function setConnectError(msg){ $('#connectError').textContent = msg; }

function connectThen(afterOpenFn){
  setConnectError('');
  const url = $('#serverInput').value.trim();
  if (ws) { try { ws.close(); } catch(e){} }
  ws = new WebSocket(url);
  ws.addEventListener('open', afterOpenFn);
  ws.addEventListener('message', onMessage);
  ws.addEventListener('close', () => setConnectError('Disconnected from server.'));
  ws.addEventListener('error', () => setConnectError('Could not reach server. Check the address.'));
}

function send(payload){
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
}

// ---------------------------------------------------------------- routing --

function onMessage(evt){
  const msg = JSON.parse(evt.data);
  switch(msg.type){
    case 'error':
      setConnectError(msg.message);
      if (roomState?.phase === 'lobby') $('#lobbyHint').textContent = msg.message;
      break;
    case 'joined': onJoined(msg); break;
    case 'room_state': onRoomState(msg); break;
    case 'toss_prompt': onTossPrompt(msg); break;
    case 'toss_result': onTossResult(msg); break;
    case 'decision_prompt': onDecisionPrompt(msg); break;
    case 'innings_start': onInningsStart(msg); break;
    case 'innings_break': onInningsBreak(msg); break;
    case 'score_update': onScoreUpdate(msg); break;
    case 'await_shake': onAwaitShake(msg); break;
    case 'ball_start': onBallStart(msg); break;
    case 'timer': onTimer(msg); break;
    case 'game_paused': onGamePaused(msg); break;
    case 'return_home': location.reload(); break;
    case 'pick_locked': onPickLocked(msg); break;
    case 'ball_result': onBallResult(msg); break;
    case 'game_over': onGameOver(msg); break;
  }
}

function onJoined(msg){
  myPid = msg.pid;
  isHost = !!msg.is_host;
  $('#btnPause').disabled = false;
  $('#roomCodeLabel').textContent = msg.code;
  $('#roomCodeLabel2').textContent = msg.code;
  showScreen('screen-lobby');
}

// ------------------------------------------------------------------ lobby --

function onRoomState(msg){
  roomState = msg;
  if (msg.phase === 'lobby') {
    $('#pauseModal').classList.remove('show');
    showScreen('screen-lobby');
    renderLobby(msg);
  } else if (msg.phase === 'toss' || msg.phase === 'decision') {
    showScreen('screen-toss');
    if (msg.phase === 'toss' && msg.toss?.caller_pid) {
      onTossPrompt(msg.toss);
    } else if (msg.phase === 'decision' && msg.toss?.winner_team) {
      onDecisionPrompt({
        team: msg.toss.winner_team,
        leader_pid: msg.squads[msg.toss.winner_team]?.leader,
      });
    }
  } else if (msg.phase === 'innings' || msg.phase === 'break') {
    showScreen('screen-game');
    updateRoleBadges(msg.bat_team, msg.bowl_team);
    updateMatchSettingsMeta();
  }
}

function renderLobby(state){
  renderSquadList('#battingSquadList', state, 'batting');
  renderSquadList('#bowlingSquadList', state, 'bowling');
  const me = state.players.find(player => player.id === myPid);
  if (me && document.activeElement !== $('#lobbyNameInput')) {
    $('#lobbyNameInput').value = me.name;
  }

  $('#oversDisplay').textContent = state.overs
    ? `Overs set to ${state.overs}.`
    : '';
  if (state.overs) $('#oversInput').value = state.overs;
  const difficulty = state.difficulty || 'medium';
  const ballSeconds = state.ball_seconds || 15;
  const difficultyInput = document.querySelector(`input[name="difficulty"][value="${difficulty}"]`);
  if (difficultyInput) difficultyInput.checked = true;
  $('#timerInput').value = ballSeconds;
  $('#settingsDisplay').textContent = `${difficulty[0].toUpperCase()}${difficulty.slice(1)} bot, ${ballSeconds} seconds per ball.`;
  $('#oversInput').style.display = (myPid === state.host) ? 'block' : 'none';
  $('#matchSettings').style.display = (myPid === state.host) ? 'flex' : 'none';
  $('#btnStartMatch').style.display = (myPid === state.host) ? 'inline-block' : 'none';

  const batCount = state.squads.batting.order.length;
  const bowlCount = state.squads.bowling.order.length;
  $('#lobbyHint').textContent = (myPid === state.host)
    ? `${batCount} in Team 1, ${bowlCount} in Team 2. Apply settings, then start when ready.`
    : `Waiting for host to start… (${batCount} batting, ${bowlCount} bowling)`;
}

$('#lobbyNameInput').addEventListener('change', () => {
  const name = $('#lobbyNameInput').value.trim();
  if (name) send({ type: 'set_name', name });
});

function renderSquadList(sel, state, team){
  const squad = state.squads[team];
  const container = $(sel);
  container.innerHTML = '';
  squad.order.forEach(pid => {
    const p = state.players.find(pl => pl.id === pid);
    if (!p) return;
    const row = document.createElement('div');
    row.className = 'player-row' + (squad.leader === pid ? ' leader' : '');
    row.innerHTML = `<span>${escapeHtml(p.name)}${p.is_bot ? ' 🤖' : ''}</span>` +
      (squad.leader === pid ? '<span class="tag">LEADER</span>' : '');
    container.appendChild(row);
  });
}

$all('[data-team]').forEach(btn => {
  btn.addEventListener('click', () => {
    const name = $('#lobbyNameInput').value.trim();
    if (name) send({ type: 'set_name', name });
    send({ type: 'join_team', team: btn.dataset.team });
  });
});

function readMatchSettings(){
  const difficulty = document.querySelector('input[name="difficulty"]:checked')?.value || 'medium';
  const rawSeconds = parseInt($('#timerInput').value, 10);
  const rawOvers = parseInt($('#oversInput').value, 10);
  const ballSeconds = Math.max(5, Math.min(Number.isFinite(rawSeconds) ? rawSeconds : 15, 15));
  const overs = Math.max(1, Math.min(Number.isFinite(rawOvers) ? rawOvers : 1, 20));
  $('#timerInput').value = ballSeconds;
  $('#oversInput').value = overs;
  return { difficulty, ball_seconds: ballSeconds, overs };
}

$('#btnSetSettings').addEventListener('click', () => {
  const settings = readMatchSettings();
  send({ type: 'set_settings', ...settings, name: $('#lobbyNameInput').value.trim() });
  if (roomState) {
    roomState.difficulty = settings.difficulty;
    roomState.ball_seconds = settings.ball_seconds;
    const me = roomState.players.find(player => player.id === myPid);
    if (me && $('#lobbyNameInput').value.trim()) me.name = $('#lobbyNameInput').value.trim();
  }
  const label = settings.difficulty[0].toUpperCase() + settings.difficulty.slice(1);
  $('#settingsDisplay').textContent = `${settings.overs} overs, ${label} bot, ${settings.ball_seconds} seconds per ball.`;
  updateMatchSettingsMeta();
  showSettingsToast();
});

let settingsToastTimer = null;

function showSettingsToast(){
  const toast = $('#settingsToast');
  toast.classList.add('show');
  clearTimeout(settingsToastTimer);
  settingsToastTimer = setTimeout(() => toast.classList.remove('show'), 2400);
}

$('#btnStartMatch').addEventListener('click', () => {
  const settings = readMatchSettings();
  send({ type: 'start_match', ...settings, name: $('#lobbyNameInput').value.trim() });
});

$('#btnExitRoom').addEventListener('click', () => {
  if (!window.confirm('Exit this room?')) return;
  if (ws) ws.close();
  location.reload();
});

// ------------------------------------------------------------------- toss --

function onTossPrompt(msg){
  showScreen('screen-toss');
  $('#coin').classList.remove('flipping');
  $('.toss-assignments').classList.remove('show');
  $('#team1Toss').textContent = 'Team 1: waiting';
  $('#team2Toss').textContent = 'Team 2: waiting';
  const canCall = !msg.caller_pid || msg.caller_pid === myPid;
  $('#tossStatus').textContent = canCall
    ? 'Choose Heads or Tails. The first valid choice starts the toss.'
    : 'The other team is choosing Heads or Tails…';
  $('#tossCallButtons').style.display = 'flex';
  $('#tossCallButtons').querySelectorAll('button').forEach(button => {
    button.disabled = !canCall;
  });
  $('#tossDecisionButtons').style.display = 'none';
}

$('#tossCallButtons').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-call]');
  if (!btn || btn.disabled) return;
  send({ type: 'toss_call', call: btn.dataset.call });
  $('#coin').classList.add('flipping');
  $('#tossStatus').textContent = 'Flipping…';
});

function onTossResult(msg){
  $('#coin').classList.add('flipping');
  const winnerLabel = msg.winner_team === 'batting' ? 'Team 1' : 'Team 2';
  const team1Call = msg.caller_team === 'batting' ? msg.call : msg.opponent_call;
  const team2Call = msg.caller_team === 'bowling' ? msg.call : msg.opponent_call;
  $('.toss-assignments').classList.add('show');
  $('#team1Toss').textContent = `Team 1: ${team1Call.toUpperCase()}`;
  $('#team2Toss').textContent = `Team 2: ${team2Call.toUpperCase()}`;
  $('#tossCallButtons').querySelectorAll('button').forEach(button => {
    button.disabled = true;
  });
  const isCaller = roomState?.players?.find(player => player.id === myPid)?.team === msg.caller_team;
  const ownCall = isCaller ? msg.call : msg.opponent_call;
  const otherCall = isCaller ? msg.opponent_call : msg.call;
  $('#tossStatus').textContent = `${ownCall.toUpperCase()} for you, ${otherCall.toUpperCase()} for the opponent. ${msg.result.toUpperCase()}! ${winnerLabel} won the toss.`;
}

function onDecisionPrompt(msg){
  const isDecider = msg.leader_pid === myPid;
  $('#tossDecisionButtons').style.display = 'none';
  $('#tossStatus').textContent = isDecider
    ? 'Toss complete. Choose batting or bowling…'
    : 'Toss complete. Opponent won and chooses batting or bowling…';
  setTimeout(() => {
    $('#tossDecisionButtons').style.display = isDecider ? 'flex' : 'none';
    $('#tossStatus').textContent = isDecider
      ? 'You won the toss! Choose batting or bowling.'
      : 'Opponent won the toss and chooses batting or bowling…';
  }, 1200);
}

$('#tossDecisionButtons').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-decision]');
  if (!btn) return;
  send({ type: 'toss_decision', decision: btn.dataset.decision });
});

// ---------------------------------------------------------------- innings --

function onInningsStart(msg){
  if (roomState) {
    if (msg.difficulty) roomState.difficulty = msg.difficulty;
    if (msg.ball_seconds) roomState.ball_seconds = msg.ball_seconds;
  }
  showScreen('screen-game');
  updateRoleBadges(msg.bat_team, msg.bowl_team);
  updateMatchSettingsMeta();
}

function onInningsBreak(msg){
  $('#resultBanner').innerHTML =
    `<b>Innings break.</b> First innings: ${msg.first_innings_score} runs. Target: ${msg.first_innings_score + 1}.`;
  updateRoleBadges(msg.next_bat_team, msg.next_bowl_team);
  if (msg.next_batter === myPid) {
    $('#resultBanner').innerHTML += ' You are batting now.';
  } else if (msg.next_bowler === myPid) {
    $('#resultBanner').innerHTML += ' You are bowling now.';
  }
}

function updateRoleBadges(batTeam, bowlTeam){
  $('#battingRoleBadge').textContent = batTeam === 'batting' ? 'Batting now' : 'Bowling now';
  $('#bowlingRoleBadge').textContent = bowlTeam === 'bowling' ? 'Bowling now' : 'Batting now';
}

function updateMatchSettingsMeta(){
  const difficulty = roomState?.difficulty || 'medium';
  const seconds = roomState?.ball_seconds || 15;
  $('#matchSettingsMeta').textContent = `${difficulty[0].toUpperCase()}${difficulty.slice(1)} · ${seconds}s per ball`;
}

function onScoreUpdate(msg){
  $('#scoreRuns').firstChild.textContent = msg.runs;
  $('#scoreWickets').textContent = `/${msg.wickets}`;
  const oversFace = `${Math.floor(msg.balls / 6)}.${msg.balls % 6}`;
  $('#overInfo').textContent = `Over ${oversFace} of ${msg.overs}`;
  $('#targetInfo').textContent = msg.target ? `Target: ${msg.target + 1}` : '';
  $('#oversMeta').textContent = `Overs: ${msg.overs}`;

  highlightActivePlayer('#battingListGame', msg.current_batter, roomState?.squads?.batting, roomState);
  highlightActivePlayer('#bowlingListGame', msg.current_bowler, roomState?.squads?.bowling, roomState);
}

function highlightActivePlayer(sel, activePid, squad, state){
  const container = $(sel);
  if (!container || !squad || !state) return;
  container.innerHTML = '';
  squad.order.forEach(pid => {
    const p = state.players.find(pl => pl.id === pid);
    if (!p) return;
    const row = document.createElement('div');
    let cls = 'player-row';
    if (pid === activePid) cls += ' active';
    if (p.is_out) cls += ' out';
    if (squad.leader === pid) cls += ' leader';
    row.className = cls;
    row.innerHTML = `<span>${escapeHtml(p.name)}${p.is_bot ? ' 🤖' : ''}</span>` +
      (pid === activePid ? '<span class="tag">NOW</span>' : '');
    container.appendChild(row);
  });
}

// ------------------------------------------------------------------- ball --

let myRoleThisBall = null; // 'batter' | 'bowler' | null
let matchSecondsThisBall = 15;
let batterPidThisBall = null;
let bowlerPidThisBall = null;
let ballAwaitingShake = false;
let ballPicking = false;
let revealTimer = null;

function onAwaitShake(msg){
  ballAwaitingShake = true;
  ballPicking = false;
  resetHands();
  moveRoleControls(msg.batter, msg.bowler);
  $('#callout').classList.remove('show');
  setPadEnabled('#batPad', false);
  setPadEnabled('#bowlPad', false);
  clearPicked();

  batterPidThisBall = msg.batter;
  bowlerPidThisBall = msg.bowler;
  myRoleThisBall = (msg.batter === myPid) ? 'batter' : (msg.bowler === myPid) ? 'bowler' : null;
  $('#batPadLabel').textContent = isBotPid(msg.batter)
    ? 'Opponent is batting automatically'
    : (msg.batter === myPid ? 'Pick your shot' : 'Waiting for batter…');
  $('#bowlPadLabel').textContent = isBotPid(msg.bowler)
    ? 'Bot is choosing automatically'
    : (msg.bowler === myPid ? 'Pick your delivery' : 'Waiting for bowler…');
  const canShake = (msg.bowler === myPid);
  $('#shakeBtn').disabled = !canShake;
  $('#resultBanner').textContent = canShake
    ? (isBotPid(msg.batter) ? 'You are bowling. Starting the ball…' : 'You are bowling. Tap SHAKE to start this ball.')
    : 'Waiting for the bowler to shake…';
  if (canShake && isBotPid(msg.batter)) {
    setTimeout(() => {
      if ($('#shakeBtn').disabled === false) $('#shakeBtn').click();
    }, 300);
  }
}

function isBotPid(pid){
  return roomState?.players?.some(player => player.id === pid && player.is_bot) || false;
}

function moveRoleControls(batterPid, bowlerPid){
  const batter = roomState?.players?.find(player => player.id === batterPid);
  const bowler = roomState?.players?.find(player => player.id === bowlerPid);
  const batterPanel = batter?.team === 'bowling' ? '#bowlingPanel' : '#battingPanel';
  const bowlerPanel = bowler?.team === 'bowling' ? '#bowlingPanel' : '#battingPanel';
  $(batterPanel).appendChild($('#batControls'));
  $(bowlerPanel).appendChild($('#bowlControls'));
}

$('#shakeBtn').addEventListener('click', () => {
  send({ type: 'shake' });
  $('#shakeBtn').disabled = true;
});

function onBallStart(msg){
  ballAwaitingShake = false;
  ballPicking = true;
  matchSecondsThisBall = msg.seconds || roomState?.ball_seconds || 15;
  $('#timerNum').textContent = matchSecondsThisBall;
  $('#timerCircle').style.strokeDashoffset = 0;
  if (myRoleThisBall === 'batter') setPadEnabled('#batPad', true);
  if (myRoleThisBall === 'bowler') setPadEnabled('#bowlPad', true);
  $('#hands').classList.add('shaking');
  $('#resultBanner').textContent = 'Get ready…';
  setTimeout(() => {
    $('#hands').classList.remove('shaking');
    if (myRoleThisBall) $('#resultBanner').textContent = 'Pick before the timer runs out!';
  }, 1600);
}

function onTimer(msg){
  $('#timerNum').textContent = msg.seconds;
  const CIRC = 264;
  $('#timerCircle').style.strokeDashoffset = CIRC * (1 - msg.seconds / matchSecondsThisBall);
}

function onPickLocked(msg){
  if ((msg.role === 'batter' && myRoleThisBall === 'batter') ||
      (msg.role === 'bowler' && myRoleThisBall === 'bowler')){
    // own pick already shown as picked locally
  }
}

$('#batPad').addEventListener('click', (e) => handlePadClick(e, '#batPad', 'batter'));
$('#bowlPad').addEventListener('click', (e) => handlePadClick(e, '#bowlPad', 'bowler'));

function handlePadClick(e, padSel, role){
  const btn = e.target.closest('button');
  if (!btn || btn.disabled || myRoleThisBall !== role) return;
  $(padSel).querySelectorAll('button').forEach(b => b.classList.remove('picked'));
  btn.classList.add('picked');
  setPadEnabled(padSel, false);
  send({ type: 'pick', value: btn.dataset.v });
}

function setPadEnabled(sel, enabled){
  $(sel).querySelectorAll('button').forEach(b => b.disabled = !enabled);
}

function clearPicked(){
  $all('.numpad button').forEach(b => b.classList.remove('picked'));
}

function onBallResult(msg){
  ballPicking = false;
  const batterIsSquad1 = roomState?.players?.find(player => player.id === batterPidThisBall)?.team === 'batting';
  setHandPose('#handLeft', batterIsSquad1 ? msg.batter_pick : msg.bowler_pick);
  setHandPose('#handRight', batterIsSquad1 ? msg.bowler_pick : msg.batter_pick);

  let label, detail;
  if (msg.result === 'wide'){
    label = 'WIDE!';
    detail = 'Bowler missed the window — +1 run.';
  } else if (msg.result === 'out'){
    label = 'OUT!';
    detail = (msg.batter_pick === null)
      ? 'Batter missed the window.'
      : `Both went with ${formatPick(msg.batter_pick)}.`;
  } else if (msg.result === 'dot'){
    label = 'SAFE';
    detail = `Batter: ${formatPick(msg.batter_pick)} · Bowler: ${formatPick(msg.bowler_pick)} — no run.`;
  } else {
    label = `+${msg.runs} RUN${msg.runs === 1 ? '' : 'S'}`;
    detail = `Batter: ${formatPick(msg.batter_pick)} · Bowler: ${formatPick(msg.bowler_pick)}.`;
  }
  clearTimeout(revealTimer);
  $('#resultBanner').textContent = 'Revealing hands…';
  revealTimer = setTimeout(() => {
    showCallout(label);
    $('#resultBanner').innerHTML = `<b>${label}</b> ${detail}`;
  }, 850);

  setPadEnabled('#batPad', false);
  setPadEnabled('#bowlPad', false);
}

$('#btnPause').addEventListener('click', () => {
  send({ type: 'toggle_pause' });
  $('#pauseModal').classList.add('show');
});

$('#btnResume').addEventListener('click', () => {
  send({ type: 'toggle_pause' });
  $('#pauseModal').classList.remove('show');
});

$('#btnPauseExit').addEventListener('click', () => {
  $('#pauseModal').classList.remove('show');
  send({ type: 'return_to_lobby' });
});

function onGamePaused(msg){
  const paused = !!msg.paused;
  $('#btnPause').textContent = paused ? 'Resume' : 'Pause';
  $('#resultBanner').textContent = paused ? 'Game paused' : 'Game resumed';
  $('#hands').classList.toggle('paused', paused);
  setPadEnabled('#batPad', !paused && ballPicking && myRoleThisBall === 'batter' && !document.querySelector('#batPad .picked'));
  setPadEnabled('#bowlPad', !paused && ballPicking && myRoleThisBall === 'bowler' && !document.querySelector('#bowlPad .picked'));
  $('#shakeBtn').disabled = paused || !ballAwaitingShake || myRoleThisBall !== 'bowler';
}

function formatPick(v){
  if (v === null || v === undefined) return 'nothing';
  return v === 'stroke' ? 'Stroke' : v;
}

function showCallout(text){
  const el = $('#callout');
  el.textContent = text;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 1600);
}

function setHandPose(sel, value){
  const hand = $(sel);
  if (value === null || value === undefined || value === 'stroke'){
    hand.removeAttribute('data-pose');
  } else {
    hand.setAttribute('data-pose', value);
  }
}

function resetHands(){
  $('#handLeft').removeAttribute('data-pose');
  $('#handRight').removeAttribute('data-pose');
}

// --------------------------------------------------------------- game over --

function onGameOver(msg){
  showScreen('screen-gameover');
  const winnerLabel = msg.winner_team === 'batting' ? 'Team 1'
    : msg.winner_team === 'bowling' ? 'Team 2'
    : 'Nobody';
  $('#winnerHeading').textContent = msg.winner_team ? `${winnerLabel} wins!` : "It's a tie!";
  $('#winnerSummary').textContent =
    `${msg.summary} — First innings: ${msg.first_innings_score}, Second innings: ${msg.second_innings_score}.`;
}

$('#btnBackToLobby').addEventListener('click', () => {
  if (ws) ws.close();
  location.reload();
});

// ---------------------------------------------------------------- helpers --

function escapeHtml(s){
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
