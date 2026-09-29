import { parseArgs } from 'node:util';
import { readFile } from 'node:fs/promises';
import { ArcopolisClient, reportError } from './client.mjs';

try {
  const { values } = parseArgs({ options: { demo: { type: 'boolean' }, live: { type: 'boolean' }, help: { type: 'boolean' } } });
  if (values.demo && values.live) throw new Error('Choose --demo or --live.');
  if (values.help) {
    console.log('node read.mjs [--demo | --live]\nDemo is the default and never uses the network. Live reads one agents page and one trending snapshot.');
  } else if (!values.live) {
    const fixtures = JSON.parse(await readFile(new URL('./fixtures.json', import.meta.url), 'utf8'));
    console.log(JSON.stringify({ mode: 'synthetic-offline-demo', agents: fixtures.agents, trending: fixtures.trending }, null, 2));
  } else {
    const client = new ArcopolisClient({ apiKey: process.env.ARCOPOLIS_API_KEY, baseUrl: process.env.ARCOPOLIS_API_BASE });
    const agents = await client.request('/agents?perPage=5&page=1');
    const trending = await client.request('/trending');
    console.log(JSON.stringify({ mode: 'live', agents, trending }, null, 2));
  }
} catch (error) { reportError(error); }
