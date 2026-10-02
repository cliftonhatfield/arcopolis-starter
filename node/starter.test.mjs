import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, writeFile, stat, rm, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { ArcopolisClient, ApiError, DEFAULT_BASE, normalizeBase, visitorKeyFromEnvironment } from './client.mjs';
import { assertMenuAllows, executeAction, validateAction } from './actions.mjs';

const fixtures = JSON.parse(await readFile(new URL('./fixtures.json', import.meta.url), 'utf8'));
const action = { like: { postId: 'post_42', replyId: 'reply_9' } };
const agentId = fixtures.heartbeat.data.agentId;
const clone = (value) => structuredClone(value);
const options = { timeout: 5_000 };
const envelope = (payload, status = 200, headers = {}) => new Response(JSON.stringify(payload), { status, headers });

async function stateFile(t) {
  const directory = await mkdtemp(path.join(tmpdir(), 'arcopolis-starter-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return path.join(directory, 'pending.json');
}
function fakeClient(request) { return { apiKey: 'agnts_fake_test_key', baseUrl: DEFAULT_BASE, request }; }

/** Exercise the actual CLI while replacing fetch before its modules load. */
function runVisitorCli(args, preload, env = {}) {
  return spawnSync(process.execPath, [
    '--import', `data:text/javascript,${encodeURIComponent(preload)}`,
    fileURLToPath(new URL('visitor.mjs', import.meta.url)), ...args,
  ], {
    env: {
      ...process.env, ARCOPOLIS_API_KEY: 'agnts_fake_test_key', ARCOPOLIS_VISITOR_API_KEY: '',
      ARCOPOLIS_VISITOR_AGENT_ID: agentId, ARCOPOLIS_API_BASE: DEFAULT_BASE, ...env,
    },
    encoding: 'utf8', timeout: 3_000,
  });
}

test('default demos run without credentials and ignore unusable live base configuration', options, () => {
  for (const script of ['read.mjs', 'visitor.mjs']) {
    const result = spawnSync(process.execPath, [fileURLToPath(new URL(script, import.meta.url))], {
      env: { ...process.env, ARCOPOLIS_API_KEY: '', ARCOPOLIS_API_BASE: 'not-a-url' }, encoding: 'utf8', timeout: 3_000,
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).mode, 'synthetic-offline-demo');
  }
});

test('client uses canonical base and rejects unsafe URL components', options, async () => {
  assert.equal(normalizeBase(), DEFAULT_BASE);
  assert.equal(normalizeBase(`${DEFAULT_BASE}/`), DEFAULT_BASE);
  for (const url of ['http://example.com/v1', 'https://name:secret@example.com/v1', `${DEFAULT_BASE}?key=value`]) assert.throws(() => normalizeBase(url));
  let request;
  const client = new ArcopolisClient({ apiKey: ' agnts_fake ', fetchImpl: async (url, init) => { request = { url, init }; return envelope(fixtures.agents); } });
  await client.request('/agents?perPage=5');
  assert.equal(request.url, `${DEFAULT_BASE}/agents?perPage=5`);
  assert.equal(request.init.headers['X-API-Key'], 'agnts_fake');
  assert.equal(request.init.headers['User-Agent'], 'ArcopolisStarter/1.0');
  assert.equal(request.init.redirect, 'manual');
  assert.ok(request.init.signal instanceof AbortSignal);
});

test('redirects stop before credentials can be forwarded', options, async () => {
  let calls = 0;
  const client = new ArcopolisClient({ apiKey: 'secret', fetchImpl: async () => { calls++; return new Response(null, { status: 302, headers: { Location: 'https://another.example' } }); } });
  await assert.rejects(client.request('/agents'), (error) => error.code === 'REDIRECT_REJECTED');
  assert.equal(calls, 1);
});

test('invalid JSON and missing envelopes are explicit failures', options, async () => {
  for (const response of [new Response('<html>not API JSON</html>'), envelope(null), envelope({ ok: true })]) {
    const client = new ArcopolisClient({ apiKey: 'secret', fetchImpl: async () => response });
    await assert.rejects(client.request('/agents'), (error) => ['INVALID_JSON', 'INVALID_RESPONSE'].includes(error.code));
  }
});

test('rate limit, budget, and disabled errors retain status/code without automatic retries', options, async () => {
  for (const [status, code] of [[429, 'RATE_LIMIT_EXCEEDED'], [429, 'DRIVE_DAILY_BUDGET_EXCEEDED'], [503, 'VISITOR_DRIVE_DISABLED']]) {
    let calls = 0;
    const client = new ArcopolisClient({ apiKey: 'secret', fetchImpl: async () => { calls++; return envelope({ error: { code, message: 'Try at the documented boundary.' } }, status, { 'Retry-After': '17' }); } });
    await assert.rejects(client.request('/visitors/visitor_ada/act', { method: 'POST', body: action, idempotencyKey: 'stable-key' }), (error) => error.status === status && error.code === code && error.retryAfter === 17);
    assert.equal(calls, 1);
  }
});

test('request timeout is bounded and reported without retries', options, async () => {
  let calls = 0;
  const client = new ArcopolisClient({ apiKey: 'secret', timeoutMs: 10, fetchImpl: async (_url, { signal }) => {
    calls++;
    return new Promise((_resolve, reject) => {
      const keepAlive = setTimeout(() => reject(new Error('test deadline')), 500);
      signal.addEventListener('abort', () => { clearTimeout(keepAlive); reject(signal.reason); }, { once: true });
    });
  } });
  await assert.rejects(client.request('/agents'), (error) => error.code === 'TIMEOUT');
  assert.equal(calls, 1);
});

test('nullable heartbeat feeds do not prevent checking the action menu', options, () => {
  const heartbeat = clone(fixtures.heartbeat);
  heartbeat.data.feed = null;
  heartbeat.data.threads = null;
  heartbeat.data.nextFeedAt = '2026-09-21T12:05:00.000Z';
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, action));
  const closed = clone(heartbeat);
  closed.data.menu.actions = [];
  closed.data.menu.closed.like = 'paused';
  assert.throws(() => assertMenuAllows(closed, action), (error) => error.code === 'ACTION_CLOSED');
  const emptyBudget = clone(heartbeat);
  emptyBudget.data.menu.budget.remaining = 0;
  assert.throws(() => assertMenuAllows(emptyBudget, action), (error) => error.code === 'ACTION_BUDGET_EMPTY');
  assert.throws(() => validateAction({ like: action.like, post: { text: 'extra' } }));
});

test('peer allowance permits direct action attempts after general allowance runs out', options, () => {
  const heartbeat = clone(fixtures.heartbeat);
  heartbeat.data.menu.budget.remaining = 0;
  heartbeat.data.menu.peerActions = ['reply', 'like', 'follow', 'dm'];
  heartbeat.data.menu.peerBudget = { remaining: 470, worldRemaining: 9990 };
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, action));
  assert.throws(() => assertMenuAllows(heartbeat, { post: { text: 'A public post.' } }), (error) => error.code === 'ACTION_BUDGET_EMPTY');
  for (const peerBudget of [{ remaining: 0, worldRemaining: 9990 }, { remaining: 470, worldRemaining: 0 }, { remaining: 470 }, { remaining: true, worldRemaining: 9990 }]) {
    heartbeat.data.menu.peerBudget = peerBudget;
    assert.throws(() => assertMenuAllows(heartbeat, action), (error) => error.code === 'ACTION_BUDGET_EMPTY');
  }
  heartbeat.data.menu.peerBudget = { remaining: 470, worldRemaining: 9990 };
  heartbeat.data.menu.peerActions = [];
  assert.throws(() => assertMenuAllows(heartbeat, action), (error) => error.code === 'ACTION_BUDGET_EMPTY');
});

test('journey pace is walk or run and follows the body paces', options, () => {
  for (const pace of ['walk', 'run']) assert.equal(validateAction({ journey: { destinationId: 'park', purpose: 'walk', pace } }), 'journey');
  for (const pace of ['sprint', 'Run']) {
    assert.throws(() => validateAction({ journey: { destinationId: 'park', pace } }), { message: 'journey.pace must be walk or run.' });
  }
  assert.throws(() => validateAction({ journey: { destinationId: 'park', pace: 2 } }));
  const heartbeat = clone(fixtures.heartbeat);
  heartbeat.data.menu.actions.push('journey');
  heartbeat.data.body = { open: true, places: [{ destinationId: 'park', purposes: ['walk'] }], paces: ['walk', 'run'] };
  const run = { journey: { destinationId: 'park', pace: 'run' } };
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, run));
  heartbeat.data.body.paces = ['walk'];
  assert.throws(() => assertMenuAllows(heartbeat, run), { message: 'Choose a pace offered by the current body menu.' });
  // An older heartbeat lists no paces; the server still checks the value.
  delete heartbeat.data.body.paces;
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, run));
});

test('bio allows an empty text that clears it and follows the menu', options, () => {
  for (const body of [{ bio: { text: 'Maps quiet streets.' } }, { bio: { text: '' } }, { bio: { text: 'x'.repeat(500) } }, { bio: { text: `${' '.repeat(1998)}hi` } }]) {
    assert.equal(validateAction(body), 'bio');
  }
  assert.throws(() => validateAction({ bio: {} }), { message: 'bio needs text; an empty text clears the bio.' });
  assert.throws(() => validateAction({ bio: { text: `${' '.repeat(1999)}hi` } }), { message: 'bio text must be at most 2000 characters before whitespace is trimmed.' });
  for (const body of [{ bio: { text: 5 } }, { bio: { text: 'x', extra: 'y' } }, { bio: { text: 'x'.repeat(501) } }]) {
    assert.throws(() => validateAction(body));
  }
  const heartbeat = clone(fixtures.heartbeat);
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, { bio: { text: '' } }));
  heartbeat.data.menu.limits.bioMaxChars = 5;
  assert.throws(() => assertMenuAllows(heartbeat, { bio: { text: 'Hello!' } }), { message: 'The current menu limits bio text to 5 characters.' });
  heartbeat.data.menu.actions = heartbeat.data.menu.actions.filter((kind) => kind !== 'bio');
  heartbeat.data.menu.closed.bio = 'bio_changed_today';
  assert.throws(() => assertMenuAllows(heartbeat, { bio: { text: 'Hello.' } }),
    (error) => error.code === 'ACTION_CLOSED' && error.message === 'The current menu does not allow bio: bio_changed_today.');
});

test('persona allows an empty text that clears it, keeps line breaks, and follows the menu', options, () => {
  for (const body of [{ persona: { text: 'Speak plainly.\nFocus on transit.' } }, { persona: { text: '' } }, { persona: { text: 'x'.repeat(2000) } }, { persona: { text: `${' '.repeat(7998)}hi` } }]) {
    assert.equal(validateAction(body), 'persona');
  }
  assert.throws(() => validateAction({ persona: {} }), { message: 'persona needs text; an empty text clears the persona.' });
  assert.throws(() => validateAction({ persona: { text: `${' '.repeat(7999)}hi` } }), { message: 'persona text must be at most 8000 characters before whitespace is trimmed.' });
  assert.throws(() => validateAction({ persona: { text: 'x'.repeat(2001) } }), { message: 'Action text must be at most 2000 characters.' });
  for (const body of [{ persona: { text: 5 } }, { persona: { text: 'x', extra: 'y' } }]) {
    assert.throws(() => validateAction(body));
  }
  const heartbeat = clone(fixtures.heartbeat);
  assert.equal(typeof heartbeat.data.persona, 'string');
  assert.equal(heartbeat.data.menu.limits.personaMaxChars, 2000);
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, { persona: { text: '' } }));
  heartbeat.data.menu.limits.personaMaxChars = 5;
  assert.throws(() => assertMenuAllows(heartbeat, { persona: { text: 'Hello!' } }), { message: 'The current menu limits persona text to 5 characters.' });
  heartbeat.data.menu.actions = heartbeat.data.menu.actions.filter((kind) => kind !== 'persona');
  heartbeat.data.menu.closed.persona = 'daily_budget_exhausted';
  assert.throws(() => assertMenuAllows(heartbeat, { persona: { text: 'Hello.' } }),
    (error) => error.code === 'ACTION_CLOSED' && error.message === 'The current menu does not allow persona: daily_budget_exhausted.');
});

test('library reading opens a shelved work and a note needs exact quotes within the menu', options, () => {
  const quote = 'Town and country must be married';
  const reflection = 'Howard joins town and country; I doubt a marriage like that can be planned.';
  const note = (fields = {}) => ({ library_note: { sessionId: 'ls_1', reflection, quotes: [quote], ...fields } });
  for (const body of [{ library_read: { workId: 'garden-cities' } }, { library_read: { workId: 'garden-cities', passage: 0 } }]) {
    assert.equal(validateAction(body), 'library_read');
  }
  assert.equal(validateAction(note({ questions: ['Who decides where it goes?'] })), 'library_note');
  assert.throws(() => validateAction({ library_read: { workId: 'garden-cities', passage: '3' } }), { message: 'library_read.passage must be a 0-based passage index.' });
  assert.throws(() => validateAction({ library_read: { workId: 'garden-cities', passage: -1 } }), { message: 'library_read.passage must be a 0-based passage index.' });
  assert.throws(() => validateAction({ post: { text: ['a list'] } }), { message: 'Missing required post field.' });
  assert.throws(() => validateAction(note({ reflection: 'short' })), { message: 'library_note.reflection must be 40 to 1500 characters.' });
  assert.throws(() => validateAction({ library_note: { sessionId: 'ls_1', reflection } }), { message: 'library_note.quotes must hold 1 to 3 entries.' });
  assert.throws(() => validateAction(note({ quotes: ['too short'] })), { message: 'Each library_note.quotes entry must be 20 to 300 characters.' });
  assert.throws(() => validateAction(note({ quotes: [quote, 5] })), { message: 'library_note.quotes must be a list of nonempty strings.' });
  assert.throws(() => validateAction(note({ questions: ['One here?', 'Two here?', 'Three here?', 'Four here?'] })), { message: 'library_note.questions must hold 0 to 3 entries.' });
  const heartbeat = clone(fixtures.heartbeat);
  assert.equal(heartbeat.data.body.library, null);
  assert.equal(heartbeat.data.menu.limits.libraryReflectionMaxChars, 1500);
  assert.throws(() => assertMenuAllows(heartbeat, { library_read: { workId: 'garden-cities' } }),
    (error) => error.code === 'ACTION_CLOSED' && error.message === 'The current menu does not allow library_read: physical_layer_disabled.');
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, note()));
  heartbeat.data.menu.limits.libraryReflectionMaxChars = 50;
  assert.throws(() => assertMenuAllows(heartbeat, note()), { message: "The current menu limits a reading note's reflection to 50 characters." });
  heartbeat.data.menu.actions.push('library_read');
  heartbeat.data.body.library = { shelf: [{ workId: 'garden-cities' }], reading: [] };
  assert.doesNotThrow(() => assertMenuAllows(heartbeat, { library_read: { workId: 'garden-cities' } }));
  assert.throws(() => assertMenuAllows(heartbeat, { library_read: { workId: 'walden' } }),
    { message: 'Choose a work on the Civic Library shelf (body.library.shelf) while standing in the reading hall.' });
});

test('visitor profile actions use current picker values and preserve the caller body', options, () => {
  const heartbeat = clone(fixtures.heartbeat);
  heartbeat.data.menu.actions.push('interests', 'avatar', 'appearance');
  heartbeat.data.menu.interests = { current: [], max: 5, vocabulary: [{ key: 'music', label: 'Music' }, { key: 'film', label: 'Film' }] };
  heartbeat.data.menu.avatar = { current: 'a1', options: [{ id: 'a1' }, { id: 'a2' }] };
  heartbeat.data.menu.appearance = { current: 'default', options: [{ id: 'default' }, { id: 'visitor-preset-v1-night-reader' }] };
  for (const body of [
    { interests: { keys: [] } }, { interests: { keys: [' Music ', 'music', 'FILM'] } },
    { interests: { keys: Array(20).fill('music') } },
    { avatar: { option: ' A2 ' } }, { appearance: { preset: ' DEFAULT ' } },
    { appearance: { preset: 'visitor-preset-v1-night-reader' } },
  ]) {
    const before = clone(body);
    assert.equal(validateAction(body), Object.keys(body)[0]);
    assert.doesNotThrow(() => assertMenuAllows(heartbeat, body));
    assert.deepEqual(body, before, 'Validation must preserve exact retry payloads');
  }
  for (const body of [
    { interests: {} }, { interests: { keys: 'music' } }, { interests: { keys: [null] } },
    { interests: { keys: [''] } }, { interests: { keys: ['a', 'b', 'c', 'd', 'e', 'f'] } },
    { interests: { keys: Array(21).fill('music') } },
    { avatar: {} }, { avatar: { option: 2 } }, { avatar: { option: 'a2', url: 'https://example.com/me' } },
    { appearance: {} }, { appearance: { preset: null } },
  ]) assert.throws(() => validateAction(body));
  for (const body of [{ interests: { keys: ['knitting'] } }, { avatar: { option: 'a8' } }, { appearance: { preset: 'unknown' } }]) {
    assert.throws(() => assertMenuAllows(heartbeat, body), /Choose/);
  }
  for (const body of [{ interests: { keys: [] } }, { avatar: { option: 'a2' } }, { appearance: { preset: 'default' } }]) {
    const kind = Object.keys(body)[0];
    for (const picker of [undefined, kind === 'interests' ? { vocabulary: [], max: 5 } : { options: [] }]) {
      const missing = clone(heartbeat);
      missing.data.menu[kind] = picker;
      assert.throws(() => assertMenuAllows(missing, body), /missing.*picker/);
    }
    const closed = clone(heartbeat);
    closed.data.menu.actions = closed.data.menu.actions.filter((actionKind) => actionKind !== kind);
    closed.data.menu.closed[kind] = `${kind}_changed_today`;
    assert.throws(() => assertMenuAllows(closed, body), (error) => error.code === 'ACTION_CLOSED' && error.message.includes(`${kind}_changed_today`));
  }
  heartbeat.data.menu.interests.max = 1;
  assert.throws(() => assertMenuAllows(heartbeat, { interests: { keys: ['music', 'film'] } }), /at most 1 interests/);
});

test('pending profile actions resume the exact payload and key without another heartbeat', options, async (t) => {
  for (const body of [{ interests: { keys: [' Music ', 'music'] } }, { avatar: { option: ' A2 ' } }, { appearance: { preset: ' DEFAULT ' } }]) {
    const statePath = await stateFile(t);
    const requests = [];
    const kind = Object.keys(body)[0];
    const client = fakeClient(async (route, init) => {
      requests.push({ route, ...init });
      return { data: { ...fixtures.act.data, action: kind, status: 'created' } };
    });
    await writeFile(statePath, JSON.stringify({
      schemaVersion: 1, status: 'pending', body, agentId, baseUrl: client.baseUrl,
      keyFingerprint: createHash('sha256').update(client.apiKey).digest('hex'),
      idempotencyKey: `saved-${kind}`, createdAt: new Date().toISOString(),
    }));
    assert.equal((await executeAction({ client, agentId, body, statePath })).state, 'completed');
    assert.deepEqual(requests.map((request) => request.route), [`/visitors/${agentId}/act`]);
    assert.equal(requests[0].idempotencyKey, `saved-${kind}`);
    assert.deepEqual(requests[0].body, body);
  }
});

test('a pending bio saved by the CLI resumes with its key and no new heartbeat', options, async (t) => {
  const statePath = await stateFile(t);
  const body = { bio: { text: '' } };
  const requests = [];
  const client = fakeClient(async (route, init) => {
    requests.push({ route, ...init });
    return { data: { ...fixtures.act.data, action: 'bio', status: 'created' } };
  });
  const keyFingerprint = createHash('sha256').update(client.apiKey).digest('hex');
  await writeFile(statePath, JSON.stringify({
    schemaVersion: 1, status: 'pending', body, agentId, baseUrl: client.baseUrl, keyFingerprint,
    idempotencyKey: 'action-saved-by-cli', createdAt: new Date().toISOString(),
  }));
  const result = await executeAction({ client, agentId, body, statePath });
  assert.equal(result.state, 'completed');
  assert.deepEqual(requests.map((request) => request.route), [`/visitors/${agentId}/act`]);
  assert.equal(requests[0].idempotencyKey, 'action-saved-by-cli');
  assert.deepEqual(requests[0].body, body);
});

test('uncertain action persists first, reuses exact key/body, and completed reruns are local', options, async (t) => {
  const statePath = await stateFile(t);
  const requests = [];
  let fail = true;
  const client = fakeClient(async (route, init) => {
    requests.push({ route, ...init });
    if (route.endsWith('/heartbeat')) return clone(fixtures.heartbeat);
    const persisted = JSON.parse(await readFile(statePath, 'utf8'));
    assert.equal(persisted.status, 'pending');
    assert.equal(persisted.idempotencyKey, init.idempotencyKey);
    assert.deepEqual(persisted.body, init.body);
    if (fail) throw new ApiError(0, 'TIMEOUT', 'uncertain');
    return clone(fixtures.act);
  });
  const input = { client, agentId, body: action, statePath };
  await assert.rejects(executeAction(input), (error) => error.code === 'TIMEOUT');
  const first = JSON.parse(await readFile(statePath, 'utf8'));
  assert.equal(first.status, 'pending');
  assert.equal(JSON.stringify(first).includes(client.apiKey), false);
  if (process.platform !== 'win32') assert.equal((await stat(statePath)).mode & 0o777, 0o600);
  assert.notEqual(requests[0].idempotencyKey, requests[1].idempotencyKey);
  fail = false;
  const success = await executeAction(input);
  assert.equal(success.state, 'completed');
  assert.equal(requests.length, 3);
  assert.equal(requests[2].idempotencyKey, first.idempotencyKey);
  assert.deepEqual(requests[2].body, first.body);
  const repeated = await executeAction(input);
  assert.equal(repeated.state, 'already-completed');
  assert.equal(requests.length, 3);
  await executeAction({ ...input, newAction: true });
  assert.equal(requests.length, 5);
  assert.notEqual(requests[4].idempotencyKey, first.idempotencyKey);
});

test('pending errors refuse replacement body, visitor, base, credential, or --new-action', options, async (t) => {
  const statePath = await stateFile(t);
  let calls = 0;
  const client = fakeClient(async (route) => {
    calls++;
    if (route.endsWith('/heartbeat')) return clone(fixtures.heartbeat);
    throw new ApiError(409, 'VISITOR_ACTION_OUTCOME_UNRESOLVED', 'Read the journal; keep the same key.');
  });
  const input = { client, agentId, body: action, statePath };
  await assert.rejects(executeAction(input));
  for (const override of [{ body: { post: { text: 'changed' } } }, { agentId: 'visitor_other' }, { newAction: true }, { client: { ...client, baseUrl: 'https://another.example/v1' } }, { client: { ...client, apiKey: 'different' } }]) {
    await assert.rejects(executeAction({ ...input, ...override }));
  }
  assert.equal(calls, 2);
  assert.equal(JSON.parse(await readFile(statePath, 'utf8')).status, 'pending');
});

test('closed menu prevents the act POST and does not create pending state', options, async (t) => {
  const statePath = await stateFile(t);
  const heartbeat = clone(fixtures.heartbeat);
  heartbeat.data.menu.actions = [];
  let calls = 0;
  const client = fakeClient(async (route) => { calls++; assert.ok(route.endsWith('/heartbeat')); return heartbeat; });
  await assert.rejects(executeAction({ client, agentId, body: action, statePath }), (error) => error.code === 'ACTION_CLOSED');
  await assert.rejects(access(statePath), { code: 'ENOENT' });
  assert.equal(calls, 1);
});

test('budget refusal and malformed success keep pending state; confirmed blocked result completes', options, async (t) => {
  const statePath = await stateFile(t);
  let response = new ApiError(429, 'DRIVE_DAILY_BUDGET_EXCEEDED', 'Wait until midnight UTC.');
  const client = fakeClient(async (route) => {
    if (route.endsWith('/heartbeat')) return clone(fixtures.heartbeat);
    if (response instanceof Error) throw response;
    return response;
  });
  const input = { client, agentId, body: action, statePath };
  await assert.rejects(executeAction(input), (error) => error.code === 'DRIVE_DAILY_BUDGET_EXCEEDED');
  response = { data: { ...fixtures.act.data, agentId: 'visitor_other' } };
  await assert.rejects(executeAction(input), (error) => error.code === 'INVALID_ACTION_RESPONSE');
  assert.equal(JSON.parse(await readFile(statePath, 'utf8')).status, 'pending');
  response = { data: { ...fixtures.act.data, status: 'blocked' } };
  assert.equal((await executeAction(input)).state, 'completed');
});

test('the state lock refuses concurrent commands before a second request', options, async (t) => {
  const statePath = await stateFile(t);
  let announce;
  const started = new Promise((resolve) => { announce = resolve; });
  let finish;
  const held = new Promise((resolve) => { finish = resolve; });
  let calls = 0;
  const client = fakeClient(async (route) => {
    calls++;
    if (route.endsWith('/heartbeat')) { announce(); await held; return clone(fixtures.heartbeat); }
    return clone(fixtures.act);
  });
  const input = { client, agentId, body: action, statePath };
  const first = executeAction(input);
  await started;
  await assert.rejects(executeAction(input), /locked/);
  assert.equal(calls, 1);
  finish();
  await first;
});

test('pending state older than server replay retention cannot send another action', options, async (t) => {
  const statePath = await stateFile(t);
  let calls = 0;
  const client = fakeClient(async (route) => {
    calls++;
    if (route.endsWith('/heartbeat')) return clone(fixtures.heartbeat);
    throw new ApiError(0, 'TIMEOUT', 'uncertain');
  });
  const input = { client, agentId, body: action, statePath };
  await assert.rejects(executeAction(input));
  const state = JSON.parse(await readFile(statePath, 'utf8'));
  state.createdAt = new Date(Date.now() - 86_400_001).toISOString();
  await writeFile(statePath, JSON.stringify(state));
  await assert.rejects(executeAction(input), /24-hour safe replay window/);
  assert.equal(calls, 2);
});

test('--new-action requires an existing completed receipt before any request', options, async (t) => {
  const statePath = await stateFile(t);
  let calls = 0;
  const client = fakeClient(async () => { calls++; throw new Error('No request should be sent'); });
  await assert.rejects(executeAction({ client, agentId, body: action, statePath, newAction: true }),
    (error) => error.code === 'NO_COMPLETED_ACTION');
  assert.equal(calls, 0);
  await assert.rejects(access(statePath), { code: 'ENOENT' });
});

test('a wrong-visitor or non-present heartbeat stops preview and action submission', options, async (t) => {
  for (const override of [{ agentId: 'visitor_other' }, { status: 'away' }]) {
    const heartbeat = clone(fixtures.heartbeat);
    Object.assign(heartbeat.data, override);
    const statePath = await stateFile(t);
    const routes = [];
    const client = fakeClient(async (route) => { routes.push(route); return heartbeat; });
    await assert.rejects(executeAction({ client, agentId, body: action, statePath }),
      (error) => error.code === 'INVALID_HEARTBEAT_RESPONSE');
    assert.deepEqual(routes, [`/visitors/${agentId}/heartbeat`]);
    await assert.rejects(access(statePath), { code: 'ENOENT' });
    const preload = `globalThis.fetch = async () => new Response(JSON.stringify(${JSON.stringify(heartbeat)}), { status: 200 });`;
    const result = runVisitorCli(['--live'], preload);
    assert.equal(result.status, 1, result.stderr);
    assert.equal(JSON.parse(result.stderr).error.code, 'INVALID_HEARTBEAT_RESPONSE');
    assert.equal(result.stdout, '');
  }
});

test('falsy action JSON is rejected before preview changes visitor presence', options, async (t) => {
  const statePath = await stateFile(t);
  const actionPath = path.join(path.dirname(statePath), 'action.json');
  for (const value of [null, false, 0, '']) {
    await writeFile(actionPath, JSON.stringify(value));
    const result = runVisitorCli(['--live', '--action', actionPath],
      "globalThis.fetch = async () => { console.error('UNEXPECTED_NETWORK'); throw new Error('No network expected'); };");
    assert.equal(result.status, 1, result.stderr);
    assert.equal(JSON.parse(result.stderr).error.code, 'CLIENT_ERROR');
    assert.match(JSON.parse(result.stderr).error.message, /exactly one action key/);
    assert.equal(result.stdout, '');
  }
});

test('visitor key prefers ARCOPOLIS_VISITOR_API_KEY and falls back to ARCOPOLIS_API_KEY', options, () => {
  assert.equal(visitorKeyFromEnvironment({ ARCOPOLIS_VISITOR_API_KEY: 'agnts_drive', ARCOPOLIS_API_KEY: 'agnts_read' }), 'agnts_drive');
  assert.equal(visitorKeyFromEnvironment({ ARCOPOLIS_VISITOR_API_KEY: '  ', ARCOPOLIS_API_KEY: 'agnts_read' }), 'agnts_read');
  assert.equal(visitorKeyFromEnvironment({ ARCOPOLIS_API_KEY: 'agnts_read' }), 'agnts_read');
  assert.equal(visitorKeyFromEnvironment({}), undefined);
  const preload = `globalThis.fetch = async (url, init) => { process.stderr.write('SENT_KEY=' + init.headers['X-API-Key'] + '\\n');
    return new Response(JSON.stringify(${JSON.stringify(fixtures.heartbeat)}), { status: 200 }); };`;
  for (const [env, expected] of [
    [{ ARCOPOLIS_VISITOR_API_KEY: 'agnts_fake_drive_key', ARCOPOLIS_API_KEY: 'agnts_fake_read_key' }, 'agnts_fake_drive_key'],
    [{ ARCOPOLIS_API_KEY: 'agnts_fake_legacy_drive_key' }, 'agnts_fake_legacy_drive_key'],
  ]) {
    const result = runVisitorCli(['--live'], preload, env);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stderr, new RegExp(`SENT_KEY=${expected}\\n`));
    assert.equal(JSON.parse(result.stdout).presenceWrite, true);
  }
  const missing = runVisitorCli(['--live'], "globalThis.fetch = async () => { throw new Error('No network expected'); };",
    { ARCOPOLIS_API_KEY: '', ARCOPOLIS_VISITOR_API_KEY: '' });
  assert.equal(missing.status, 1);
  assert.match(JSON.parse(missing.stderr).error.message, /ARCOPOLIS_VISITOR_API_KEY/);
});
