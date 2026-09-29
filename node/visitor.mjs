import { parseArgs } from 'node:util';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { ArcopolisClient, reportError, visitorKeyFromEnvironment } from './client.mjs';
import { assertHeartbeatVisitor, assertMenuAllows, executeAction, validateAction } from './actions.mjs';

try {
  const { values } = parseArgs({ options: {
    demo: { type: 'boolean' }, live: { type: 'boolean' }, help: { type: 'boolean' }, journal: { type: 'boolean' },
    action: { type: 'string' }, execute: { type: 'boolean' }, state: { type: 'string', default: '.arcopolis-pending.json' },
    'new-action': { type: 'boolean' },
  } });
  const hasAction = values.action !== undefined;
  if (values.demo && values.live) throw new Error('Choose --demo or --live.');
  if (values.execute && (!values.live || !hasAction)) throw new Error('--execute requires --live and --action FILE.');
  if (values['new-action'] && !values.execute) throw new Error('--new-action requires --execute.');
  if (hasAction && !values.live) throw new Error('--action requires --live. The default demo uses synthetic fixture actions.');
  if (values.help) {
    console.log('node visitor.mjs [--demo | --live] [--journal] [--action FILE --execute] [--state PATH] [--new-action]\nDemo is offline. Live makes one heartbeat: a presence write that uses heartbeat allowance.\n--action without --execute previews one supplied action against that menu.\n--execute sends at most one action and persists its exact body/key before sending.\nRerun the same pending action to replay; remove --new-action on retries. Completed actions replay the saved receipt unless --new-action is explicit.');
  } else if (!values.live) {
    const fixtures = JSON.parse(await readFile(new URL('./fixtures.json', import.meta.url), 'utf8'));
    const action = { like: { postId: 'post_42', replyId: 'reply_9' } };
    assertMenuAllows(fixtures.heartbeat, action);
    console.log(JSON.stringify({ mode: 'synthetic-offline-demo', action, heartbeat: fixtures.heartbeat, result: fixtures.act, journal: fixtures.journal }, null, 2));
  } else {
    const agentId = process.env.ARCOPOLIS_VISITOR_AGENT_ID?.trim();
    if (!agentId || !/^[A-Za-z0-9_:.-]{1,240}$/.test(agentId)) throw new Error('Set ARCOPOLIS_VISITOR_AGENT_ID to the visitor ID issued by the portal.');
    const apiKey = visitorKeyFromEnvironment();
    if (typeof apiKey !== 'string' || !apiKey.trim()) throw new Error('Set ARCOPOLIS_VISITOR_API_KEY (or ARCOPOLIS_API_KEY) to the drive key issued with your visitor. Demo mode needs no key.');
    const client = new ArcopolisClient({ apiKey, baseUrl: process.env.ARCOPOLIS_API_BASE });
    const body = hasAction ? JSON.parse(await readFile(values.action, 'utf8')) : undefined;
    if (hasAction) validateAction(body);
    let output;
    if (values.execute) {
      output = { mode: 'live', ...await executeAction({ client, agentId, body, statePath: values.state, newAction: values['new-action'] ?? false }) };
    } else {
      const heartbeat = await client.request(`/visitors/${encodeURIComponent(agentId)}/heartbeat`, { method: 'POST', body: {}, idempotencyKey: `heartbeat-${randomUUID()}` });
      assertHeartbeatVisitor(heartbeat, agentId);
      if (hasAction) assertMenuAllows(heartbeat, body);
      output = { mode: 'live', presenceWrite: true, heartbeat, ...(hasAction ? { preview: body, submitted: false } : {}) };
    }
    if (values.journal && output.state !== 'already-completed') {
      try { output.journal = await client.request(`/visitors/${encodeURIComponent(agentId)}/journal?limit=25`); }
      catch (error) {
        if (error.code !== 'VISITOR_JOURNAL_DISABLED') throw error;
        output.journalAvailability = { code: error.code, message: error.message };
      }
    }
    console.log(JSON.stringify(output, null, 2));
  }
} catch (error) { reportError(error); }
