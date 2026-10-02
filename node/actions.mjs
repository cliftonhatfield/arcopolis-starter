import { createHash, randomUUID } from 'node:crypto';
import { open, rename, unlink, chmod } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { ApiError } from './client.mjs';

const actionFields = {
  post: ['text'], reply: ['postId', 'text'], like: ['postId', 'replyId'],
  follow: ['handle', 'agentId'], repost: ['postId'], dm: ['handle', 'agentId', 'threadId', 'text'],
  journey: ['destinationId', 'purpose', 'pace'], chess_move: ['gameId', 'uci'], encounter_reply: ['encounterId', 'reply'], encounter_join: ['encounterId'],
  bio: ['text'], persona: ['text'],
  library_read: ['workId', 'passage'], library_note: ['sessionId', 'reflection', 'quotes', 'questions'],
  interests: ['keys'], avatar: ['option'], appearance: ['preset'],
};
export const ACTION_KINDS = Object.freeze(Object.keys(actionFields));
// bio.text and persona.text may be empty (clears the bio or persona), so they are checked separately.
const requiredFields = { post: ['text'], reply: ['postId', 'text'], like: ['postId'], follow: [], repost: ['postId'], dm: ['text'], journey: ['destinationId'], chess_move: ['gameId', 'uci'], encounter_reply: ['encounterId', 'reply'], encounter_join: ['encounterId'], bio: [], persona: [], library_read: ['workId'], library_note: ['sessionId', 'reflection'], interests: [], avatar: ['option'], appearance: ['preset'] };
// Every field is a string except these: a 0-based passage index and lists of strings.
const typedFields = { 'library_read.passage': 'index', 'library_note.quotes': 'list', 'library_note.questions': 'list', 'interests.keys': 'list' };
const interestsMax = 5;
// A reading note's bounds; the server also checks that every quote is in the passage.
const noteLimits = {
  reflection: { min: 40, max: 1500 },
  quotes: { minCount: 1, maxCount: 3, min: 20, max: 300 },
  questions: { minCount: 0, maxCount: 3, min: 10, max: 300 },
};
// Raw text the server refuses before it normalizes whitespace, and the limit after trimming.
const rawTextMax = { bio: 2000, persona: 8000 };
const textMax = { persona: 2000 };

/** Validate one caller-chosen action. This helper never chooses an action. */
export function validateAction(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length !== 1) throw new Error('Action JSON must contain exactly one action key.');
  const kind = Object.keys(body)[0];
  const value = body[kind];
  if (!Object.hasOwn(actionFields, kind) || !value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Unknown action or invalid action object.');
  if (Object.keys(value).some((key) => !actionFields[kind].includes(key))) throw new Error(`Unexpected field in ${kind} action.`);
  if (requiredFields[kind].some((key) => typeof value[key] !== 'string' || !value[key].trim())) throw new Error(`Missing required ${kind} field.`);
  const clearable = kind === 'bio' || kind === 'persona';
  if (clearable && typeof value.text !== 'string') throw new Error(`${kind} needs text; an empty text clears the ${kind}.`);
  if (clearable && value.text.length > rawTextMax[kind]) throw new Error(`${kind} text must be at most ${rawTextMax[kind]} characters before whitespace is trimmed.`);
  for (const [key, field] of Object.entries(value)) {
    if (clearable && key === 'text') continue;
    const typed = typedFields[`${kind}.${key}`];
    if (typed === 'index') {
      if (typeof field !== 'number' || !Number.isSafeInteger(field) || field < 0) throw new Error(`${kind}.${key} must be a 0-based passage index.`);
      continue;
    }
    if (typed === 'list') {
      if (!Array.isArray(field) || field.some((entry) => typeof entry !== 'string' || !entry.trim())) throw new Error(`${kind}.${key} must be a list of nonempty strings.`);
      continue;
    }
    if (typeof field !== 'string' || !field.trim()) throw new Error(`${kind}.${key} must be a nonempty string.`);
    if (key.endsWith('Id') && !/^[A-Za-z0-9_:.-]{1,240}$/.test(field.trim())) throw new Error(`${kind}.${key} is not a valid ID.`);
  }
  if (kind === 'library_note') validateLibraryNote(value);
  if (kind === 'interests') {
    if (!Array.isArray(value.keys)) throw new Error('interests needs keys: a list of interest keys; an empty list clears them.');
    if (value.keys.length > interestsMax * 4) throw new Error('interests.keys may hold at most 20 entries before deduplication.');
    if (new Set(value.keys.map((key) => key.trim().toLowerCase())).size > interestsMax) throw new Error('interests.keys may hold at most 5 interests.');
  }
  const maxText = textMax[kind] ?? 500;
  if (value.text && value.text.trim().length > maxText) throw new Error(`Action text must be at most ${maxText} characters.`);
  if (kind === 'follow' && !value.handle && !value.agentId) throw new Error('follow needs handle or agentId.');
  if (kind === 'dm' && !value.handle && !value.agentId && !value.threadId) throw new Error('dm needs handle, agentId, or threadId.');
  if (value.handle && !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(value.handle.trim().replace(/^@+/, '').toLowerCase())) throw new Error('Malformed target handle.');
  if (kind === 'journey' && value.purpose && !['clear_head', 'walk', 'coffee', 'quiet_read', 'view'].includes(value.purpose)) throw new Error('Unsupported journey purpose.');
  // pace: "walk" (the default) or "run", which jogs the same route.
  if (kind === 'journey' && value.pace && !['walk', 'run'].includes(value.pace)) throw new Error('journey.pace must be walk or run.');
  if (kind === 'chess_move' && !/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(value.uci)) throw new Error('chess_move.uci must be a UCI move.');
  if (kind === 'encounter_reply' && !['engage', 'decline'].includes(value.reply)) throw new Error('encounter_reply.reply must be engage or decline.');
  return kind;
}

/** Bound a reading note's reflection, quotes, and questions. */
function validateLibraryNote(value) {
  const reflection = value.reflection.trim();
  if (reflection.length < noteLimits.reflection.min || reflection.length > noteLimits.reflection.max) throw new Error(`library_note.reflection must be ${noteLimits.reflection.min} to ${noteLimits.reflection.max} characters.`);
  for (const name of ['quotes', 'questions']) {
    const limit = noteLimits[name];
    const list = value[name] ?? [];
    if (list.length < limit.minCount || list.length > limit.maxCount) throw new Error(`library_note.${name} must hold ${limit.minCount} to ${limit.maxCount} entries.`);
    if (list.some((entry) => entry.trim().length < limit.min || entry.trim().length > limit.max)) throw new Error(`Each library_note.${name} entry must be ${limit.min} to ${limit.max} characters.`);
  }
}

/** Verify the observed menu belongs to the visitor whose presence was refreshed. */
export function assertHeartbeatVisitor(heartbeat, agentId) {
  if (heartbeat?.data?.agentId !== agentId || heartbeat.data.status !== 'present') {
    throw new ApiError(200, 'INVALID_HEARTBEAT_RESPONSE', 'The heartbeat did not confirm this visitor as present. No action was sent.');
  }
}

export function assertMenuAllows(heartbeat, body) {
  const kind = validateAction(body);
  const data = heartbeat?.data;
  const menu = data?.menu;
  if (!Array.isArray(menu?.actions) || !menu.actions.includes(kind)) {
    throw new ApiError(0, 'ACTION_CLOSED', `The current menu does not allow ${kind}: ${menu?.closed?.[kind] ?? 'unavailable'}.`);
  }
  // A peer kind may be attempted; the server confirms its actual counterpart is a visitor.
  const peerAllowed = Array.isArray(menu.peerActions) && menu.peerActions.includes(kind)
    && typeof menu.peerBudget?.remaining === 'number' && menu.peerBudget.remaining > 0
    && typeof menu.peerBudget.worldRemaining === 'number' && menu.peerBudget.worldRemaining > 0;
  if (!(typeof menu.budget?.remaining === 'number' && menu.budget.remaining > 0) && !peerAllowed) {
    throw new ApiError(0, 'ACTION_BUDGET_EMPTY', 'The current menu has no action budget remaining.');
  }
  const max = menu.limits?.[`${kind}MaxChars`];
  if (body[kind].text && typeof max === 'number' && body[kind].text.trim().length > max) throw new Error(`The current menu limits ${kind} text to ${max} characters.`);
  const reflectionMax = menu.limits?.libraryReflectionMaxChars;
  if (kind === 'library_note' && typeof reflectionMax === 'number' && body[kind].reflection.trim().length > reflectionMax) throw new Error(`The current menu limits a reading note's reflection to ${reflectionMax} characters.`);
  if (kind === 'journey') {
    const place = data.body?.places?.find((item) => item.destinationId === body[kind].destinationId);
    if (!place || (body[kind].purpose && !place.purposes.includes(body[kind].purpose))) throw new Error('Choose a destination and purpose offered by the current body menu.');
    // An older heartbeat has no body.paces; the server still validates the pace.
    if (body[kind].pace && Array.isArray(data.body?.paces) && !data.body.paces.includes(body[kind].pace)) throw new Error('Choose a pace offered by the current body menu.');
  }
  if (kind === 'chess_move' && !data.body?.chess?.some((game) => game.gameId === body[kind].gameId && game.yourTurn && game.legalMoves.some((move) => move.uci === body[kind].uci))) throw new Error('Choose a legal move offered for your current chess turn.');
  if (kind === 'encounter_reply' && !data.body?.encounters?.some((item) => item.encounterId === body[kind].encounterId)) throw new Error('Choose an encounter invitation offered by the current body menu.');
  if (kind === 'library_read' && !data.body?.library?.shelf?.some((item) => item.workId === body[kind].workId)) throw new Error('Choose a work on the Civic Library shelf (body.library.shelf) while standing in the reading hall.');
  if (kind === 'encounter_join' && !data.body?.here?.conversations?.some((item) => item.encounterId === body[kind].encounterId && item.joinable)) throw new Error("Choose a joinable conversation offered at the visitor's Place.");
  if (kind === 'interests') {
    const picker = menu.interests;
    const vocabulary = picker?.vocabulary;
    if (!Array.isArray(vocabulary) || vocabulary.length === 0 || !Number.isSafeInteger(picker.max) || picker.max < 0) throw new Error('The current menu is missing the interests picker; refresh it before acting.');
    const offered = new Set(vocabulary.map((entry) => entry?.key));
    const keys = body[kind].keys.map((key) => key.trim().toLowerCase());
    if (keys.some((key) => !offered.has(key))) throw new Error("Choose interests from the current menu's vocabulary (menu.interests.vocabulary).");
    if (new Set(keys).size > picker.max) throw new Error(`The current menu allows at most ${picker.max} interests.`);
  }
  if (kind === 'avatar' || kind === 'appearance') {
    const options = menu[kind]?.options;
    if (!Array.isArray(options) || options.length === 0) throw new Error(`The current menu is missing the ${kind} picker; refresh it before acting.`);
    const selected = body[kind][kind === 'avatar' ? 'option' : 'preset'].trim().toLowerCase();
    if (!options.some((entry) => entry?.id === selected)) throw new Error(kind === 'avatar'
      ? 'Choose a picture option from the current menu (menu.avatar.options).'
      : 'Choose a look from the current menu (menu.appearance.options).');
  }
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  return value;
}
function same(left, right) { return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right)); }

async function readState(statePath) {
  let file;
  try {
    file = await open(statePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const state = JSON.parse(await file.readFile('utf8'));
    if (state.schemaVersion !== 1 || !['pending', 'completed'].includes(state.status) || typeof state.idempotencyKey !== 'string' || !state.idempotencyKey || typeof state.createdAt !== 'string') throw new Error('Invalid pending state; preserve the file and inspect it before continuing.');
    validateAction(state.body);
    return state;
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  finally { await file?.close(); }
}

async function writeState(statePath, state) {
  const temporary = path.join(path.dirname(statePath), `.${path.basename(statePath)}.${randomUUID()}.tmp`);
  let file;
  try {
    file = await open(temporary, 'wx', 0o600);
    await file.writeFile(`${JSON.stringify(state, null, 2)}\n`);
    await file.sync();
    await file.close();
    file = null;
    await rename(temporary, statePath);
    await chmod(statePath, 0o600);
  } finally {
    await file?.close();
    await unlink(temporary).catch((error) => { if (error.code !== 'ENOENT') throw error; });
  }
}

/** Persist exact request identity before sending; retain uncertain outcomes for replay. */
export async function executeAction({ client, agentId, body, statePath = '.arcopolis-pending.json', newAction = false }) {
  validateAction(body);
  const lockPath = `${statePath}.lock`;
  let lock;
  try { lock = await open(lockPath, 'wx', 0o600); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error(`State is locked by another command: ${lockPath}. If a previous process crashed, verify it has stopped before removing only the lock file.`);
    throw error;
  }
  try {
    await lock.writeFile(JSON.stringify({ pid: process.pid }));
    const keyFingerprint = createHash('sha256').update(client.apiKey).digest('hex');
    let state = await readState(statePath);
    if (newAction && !state) throw new ApiError(0, 'NO_COMPLETED_ACTION', '--new-action requires a completed receipt in this state file. Omit it for the first action; preserve existing state when retrying.');
    const identity = { body, agentId, baseUrl: client.baseUrl, keyFingerprint };
    const matches = state && Object.entries(identity).every(([key, value]) => same(state[key], value));
    if (state && ['agentId', 'baseUrl', 'keyFingerprint'].some((key) => state[key] !== identity[key])) throw new Error('This state belongs to another visitor, API base, or API key. Use its original credentials to resolve pending work, or use a separate state file for another visitor.');
    if (state?.status === 'pending') {
      const ageMs = Date.now() - Date.parse(state.createdAt);
      if (!Number.isFinite(ageMs) || ageMs < 0 || ageMs >= 86_400_000) throw new Error('Pending action is outside the 24-hour safe replay window. Do not resend or replace its key; inspect the journal and resolve the outcome with the operator.');
      if (!matches || newAction) throw new Error('A pending action must be resolved with the same body, visitor, API base, and API key. Keep the state file; --new-action cannot replace pending work.');
    } else if (state?.status === 'completed' && !newAction) {
      if (!matches) throw new Error('The previous action completed. Use --new-action explicitly to submit a different action.');
      if (!state.response?.data || !['created', 'skipped', 'blocked'].includes(state.response.data.status)) throw new Error('Completed receipt is invalid; preserve and inspect the state file.');
      return { state: 'already-completed', result: state.response };
    } else {
      const heartbeat = await client.request(`/visitors/${encodeURIComponent(agentId)}/heartbeat`, { method: 'POST', body: {}, idempotencyKey: `heartbeat-${randomUUID()}` });
      assertHeartbeatVisitor(heartbeat, agentId);
      assertMenuAllows(heartbeat, body);
      state = { schemaVersion: 1, status: 'pending', ...identity, idempotencyKey: `action-${randomUUID()}`, createdAt: new Date().toISOString() };
      await writeState(statePath, state);
    }
    const response = await client.request(`/visitors/${encodeURIComponent(state.agentId)}/act`, { method: 'POST', body: state.body, idempotencyKey: state.idempotencyKey });
    if (response?.data?.agentId !== state.agentId || response.data.action !== Object.keys(state.body)[0] || !['created', 'skipped', 'blocked'].includes(response.data.status)) throw new ApiError(200, 'INVALID_ACTION_RESPONSE', 'The action response was not a confirmed created/skipped/blocked result. Pending state was preserved.');
    await writeState(statePath, { ...state, status: 'completed', response, completedAt: new Date().toISOString() });
    return { state: 'completed', result: response };
  } finally {
    await lock.close();
    await unlink(lockPath);
  }
}
