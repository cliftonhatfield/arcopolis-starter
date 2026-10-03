/** Peer chess shape/menu checks. Keep the exact caller body for safe retries. */
export const PEER_CHESS_ACTION_FIELDS = Object.freeze({
  chess_challenge: ['agentId', 'handle', 'paceHours'], chess_respond: ['challengeId', 'reply'],
  chess_resign: ['gameId'], chess_draw: ['gameId', 'reply'], chess_rematch: ['gameId'],
});
export const PEER_CHESS_REQUIRED_FIELDS = Object.freeze({
  chess_challenge: [], chess_respond: ['challengeId', 'reply'], chess_resign: ['gameId'],
  chess_draw: ['gameId', 'reply'], chess_rematch: ['gameId'],
});
const row = (value) => value !== null && typeof value === 'object' && !Array.isArray(value) ? value : null;
export function peerChessActionError(kind, value) {
  if (!Object.hasOwn(PEER_CHESS_ACTION_FIELDS, kind)) return null;
  const fields = row(value);
  if (!fields) return 'Unknown action or invalid action object.';
  if (Object.keys(fields).some((key) => !PEER_CHESS_ACTION_FIELDS[kind].includes(key))) return `Unexpected field in ${kind} action.`;
  for (const key of PEER_CHESS_REQUIRED_FIELDS[kind]) {
    if (typeof fields[key] !== 'string' || !fields[key].trim()) return `Missing required ${kind} field.`;
  }
  for (const [key, value] of Object.entries(fields)) {
    if (key === 'paceHours') continue;
    if (typeof value !== 'string' || !value.trim()) return `${kind}.${key} must be a nonempty string.`;
    if (key.endsWith('Id') && !/^[A-Za-z0-9_:.-]{1,240}$/.test(value.trim())) return `${kind}.${key} is not a valid ID.`;
  }
  if (fields.handle && !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(fields.handle.trim().replace(/^@+/, '').toLowerCase())) return 'Malformed target handle.';
  if (kind === 'chess_challenge') {
    if (fields.paceHours !== 24 && fields.paceHours !== 48) return 'chess_challenge.paceHours must be 24 or 48.';
    if (fields.agentId && fields.handle) return 'chess_challenge accepts agentId or handle, not both.';
  }
  if (kind === 'chess_respond' && !['accept', 'decline', 'cancel'].includes(fields.reply)) return 'chess_respond.reply must be accept, decline, or cancel.';
  if (kind === 'chess_draw' && !['offer', 'accept', 'decline'].includes(fields.reply)) return 'chess_draw.reply must be offer, accept, or decline.';
  return null;
}
export function isPeerChessAction(kind, value) {
  return Object.hasOwn(PEER_CHESS_ACTION_FIELDS, kind)
    || (kind === 'chess_move' && typeof row(value)?.gameId === 'string' && value.gameId.trim().startsWith('vchess_'));
}
export function peerChessMenuError(data, kind, value) {
  if (!isPeerChessAction(kind, value)) return null;
  const snapshot = row(data); const menu = row(snapshot?.menu); const chess = row(snapshot?.chess);
  if (chess?.available !== true || !Array.isArray(menu?.actions) || !menu.actions.includes(kind)
    || !Array.isArray(menu.chessActions) || !menu.chessActions.includes(kind)) {
    return { code: 'ACTION_CLOSED', message: `The current menu does not allow ${kind}: visitor chess unavailable.` };
  }
  const budget = row(menu.chessBudget);
  if (!(typeof budget?.remaining === 'number' && Number.isFinite(budget.remaining) && budget.remaining > 0
    && typeof budget.worldRemaining === 'number' && Number.isFinite(budget.worldRemaining) && budget.worldRemaining > 0)) {
    return { code: 'ACTION_BUDGET_EMPTY', message: 'The current menu has no chess action budget remaining.' };
  }
  if (kind === 'chess_move') {
    const fields = row(value);
    const gameId = typeof fields?.gameId === 'string' ? fields.gameId.trim() : '';
    const uci = typeof fields?.uci === 'string' ? fields.uci.trim().toLowerCase() : '';
    const legal = Array.isArray(chess.turns) && chess.turns.some((value) => {
      const game = row(value);
      return game?.gameId === gameId && game?.yourTurn === true && Array.isArray(game.legalMoves)
        && game.legalMoves.some((move) => row(move)?.uci === uci);
    });
    if (!legal) return { code: 'INVALID_ACTION', message: 'Choose a legal move offered for your current chess turn.' };
  }
  return null;
}
