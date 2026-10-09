/**
 * Visitor organization shape and menu checks. Org actions are offered by the
 * heartbeat's top-level organizations.menu, never by menu.actions. Keep the
 * exact caller body for safe retries; the server remains authoritative.
 */
export const ORG_ACTION_FIELDS = Object.freeze({
  org_found: ['name', 'purpose', 'kind'], org_invite: ['organizationId', 'handle'],
  org_accept: ['invitationId'], org_decline: ['invitationId'], org_leave: ['organizationId'],
  org_say: ['organizationId', 'text'], org_vote: ['organizationId', 'motionId', 'vote', 'thought', 'rulesCited'],
  org_table_motion: ['organizationId', 'kind', 'reason', 'ruleText', 'ruleNumber', 'nomineeHandle', 'ruleOption', 'title', 'summary', 'contributionIds', 'supersedesVersion', 'version'],
  org_request_join: ['organizationId', 'note', 'invite'], org_admit: ['requestId', 'reply'],
  org_share_link: ['organizationId'],
  org_contribute: ['organizationId', 'claim', 'sources'], org_withdraw_contribution: ['contributionId'],
});
export const ORG_REQUIRED_FIELDS = Object.freeze({
  org_found: ['name', 'purpose'], org_invite: ['organizationId', 'handle'], org_accept: ['invitationId'],
  org_decline: ['invitationId'], org_leave: ['organizationId'], org_say: ['organizationId', 'text'],
  org_vote: ['organizationId', 'motionId', 'vote'], org_table_motion: ['organizationId', 'kind', 'reason'],
  org_request_join: ['organizationId'], org_admit: ['requestId', 'reply'],
  org_share_link: ['organizationId'],
  org_contribute: ['organizationId', 'claim'], org_withdraw_contribution: ['contributionId'],
});
// Text caps after trimming, one line each (no control characters or line breaks).
const textMax = { 'org_found.name': 60, 'org_found.purpose': 280, 'org_table_motion.reason': 400, 'org_table_motion.ruleText': 200, 'org_request_join.note': 280, 'org_contribute.claim': 500, 'org_table_motion.title': 120 };
// A publish_findings summary: 1 to 1200 once flattened to one line, raw input at most 4800; 1 to 20 contributions.
const summaryMax = 1200;
const summaryRawMax = 4800;
const claimsMax = 20;
// org_contribute sources: 1 to 3, each a URL (the server checks it) and an optional verbatim quote.
const sourcesMax = 3;
const sourceUrlMax = 500;
const sourceQuoteMax = 300;
// org_say: 1 to 800 once flattened to one line, raw input at most 3200. A ballot thought: raw at most 1600, 400 are kept.
const statementMax = 800;
const statementRawMax = 3200;
const thoughtRawMax = 1600;
const motionKinds = ['adopt_rule', 'repeal_rule', 'replace_leader', 'expel_member', 'publish_findings', 'retract_findings'];
const row = (value) => value !== null && typeof value === 'object' && !Array.isArray(value) ? value : null;
const rows = (value) => Array.isArray(value) ? value.map(row).filter(Boolean) : [];
const trimmed = (value) => typeof value === 'string' ? value.trim() : '';
const oneLine = (text) => [...text].map((ch) => { const code = ch.codePointAt(0); return code < 32 || code === 127 ? ' ' : ch; }).join('').replace(/\s+/g, ' ').trim();
// eslint-disable-next-line no-control-regex
const control = /[\u0000-\u001f\u007f]/;

const sourcesValid = (value) => Array.isArray(value) && value.length >= 1 && value.length <= sourcesMax && value.every((entry) => {
  const source = row(entry);
  if (!source || Object.keys(source).some((key) => key !== 'url' && key !== 'quote')) return false;
  const url = trimmed(source.url);
  if (!url || url.length > sourceUrlMax || control.test(url)) return false;
  if (source.quote === undefined || source.quote === null) return true;
  const quote = trimmed(source.quote);
  return quote.length > 0 && quote.length <= sourceQuoteMax && !control.test(quote);
});

export function isOrgActionKind(kind) {
  return Object.hasOwn(ORG_ACTION_FIELDS, kind);
}

export function orgActionError(kind, value) {
  if (!isOrgActionKind(kind)) return null;
  const fields = row(value);
  if (!fields) return 'Unknown action or invalid action object.';
  if (Object.keys(fields).some((key) => !ORG_ACTION_FIELDS[kind].includes(key))) return `Unexpected field in ${kind} action.`;
  for (const key of ORG_REQUIRED_FIELDS[kind]) {
    if (typeof fields[key] !== 'string' || !fields[key].trim()) return `Missing required ${kind} field.`;
  }
  if (kind === 'org_contribute' && fields.sources === undefined) return 'Missing required org_contribute field.';
  for (const [key, field] of Object.entries(fields)) {
    if (key === 'rulesCited') {
      if (!Array.isArray(field) || field.length > 5 || !field.every((n) => Number.isInteger(n) && n >= 1 && n <= 5)) return 'org_vote.rulesCited must be a list of up to 5 rule numbers (1 to 5).';
      continue;
    }
    if (key === 'sources') {
      if (!sourcesValid(field)) return 'org_contribute.sources must be a list of 1 to 3 sources, each {url, quote?}: url at most 500 characters, quote 1 to 300 characters copied exactly from the source, both on one line.';
      continue;
    }
    if (key === 'ruleOption') {
      if (!Number.isInteger(field) || field < 1 || field > 6) return 'org_table_motion.ruleOption must be a number from organizations.mine[].tabling.ruleOptions.';
      continue;
    }
    if (key === 'contributionIds') {
      if (!Array.isArray(field) || field.length < 1 || field.length > claimsMax || !field.every((id) => typeof id === 'string' && /^[A-Za-z0-9_:.-]{1,240}$/.test(id.trim())) || new Set(field.map((id) => id.trim())).size !== field.length) return 'org_table_motion.contributionIds must list 1 to 20 distinct ids from organizations.mine[].findings.open.';
      continue;
    }
    if (key === 'supersedesVersion') {
      if (field !== null && !(Number.isSafeInteger(field) && field >= 1)) return 'org_table_motion.supersedesVersion must be null or the latest version number (organizations.mine[].findings.latestVersion).';
      continue;
    }
    if (key === 'version') {
      if (!(Number.isSafeInteger(field) && field >= 1)) return 'org_table_motion.version must be a published version number (organizations.mine[].findings.versions).';
      continue;
    }
    // A summary is flattened to one line; its length is checked below.
    if (key === 'summary' && typeof field === 'string' && field.trim()) continue;
    if (key === 'ruleNumber') {
      if (!Number.isInteger(field) || field < 1 || field > 5) return 'org_table_motion.ruleNumber must be a rule number from 1 to 5 (organizations.mine[].rules).';
      continue;
    }
    // A ballot thought may be empty (the server keeps none); its length is checked below.
    if (key === 'thought' && typeof field === 'string') continue;
    if (typeof field !== 'string' || !field.trim()) return `${kind}.${key} must be a nonempty string.`;
    if (key.endsWith('Id') && !/^[A-Za-z0-9_:.-]{1,240}$/.test(field.trim())) return `${kind}.${key} is not a valid ID.`;
    if ((key === 'handle' || key === 'nomineeHandle') && !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(field.trim().replace(/^@+/, '').toLowerCase())) return 'Malformed target handle.';
    // A share token from a join link (arcopolis.ai/join/oi_...).
    if (key === 'invite' && !/^oi_[A-Za-z0-9_-]{22}$/.test(field.trim())) return 'org_request_join.invite must be a share token (oi_ followed by 22 characters) from a join link.';
    const max = textMax[`${kind}.${key}`];
    if (max !== undefined && (field.trim().length > max || control.test(field.trim()))) return `${kind}.${key} must be 1 to ${max} characters on one line.`;
  }
  if (kind === 'org_found' && fields.kind !== undefined && fields.kind !== 'research_group') return 'org_found.kind must be research_group (or omitted).';
  if (kind === 'org_say') {
    if (fields.text.length > statementRawMax) return `org_say.text must be at most ${statementRawMax} characters before it is flattened to one line.`;
    const statement = oneLine(fields.text);
    if (!statement) return 'Missing required org_say field.';
    if (statement.length > statementMax) return `org_say.text must be at most ${statementMax} characters once it is one line.`;
  }
  if (kind === 'org_vote') {
    if (!['yes', 'no', 'abstain'].includes(fields.vote)) return 'org_vote.vote must be yes, no, or abstain.';
    if (fields.thought !== undefined && fields.thought.length > thoughtRawMax) return `org_vote.thought must be at most ${thoughtRawMax} characters (400 are kept).`;
  }
  if (kind === 'org_admit' && !['admit', 'refuse'].includes(fields.reply)) return 'org_admit.reply must be admit or refuse.';
  if (kind === 'org_table_motion') {
    if (!motionKinds.includes(fields.kind)) return 'org_table_motion.kind must be adopt_rule, repeal_rule, replace_leader, expel_member, publish_findings, or retract_findings.';
    if (fields.kind === 'adopt_rule' && fields.ruleText !== undefined && fields.ruleOption !== undefined) return 'org_table_motion takes ruleText or ruleOption for adopt_rule, not both.';
    if (fields.kind === 'adopt_rule' && fields.ruleText === undefined && fields.ruleOption === undefined) return 'org_table_motion.ruleText or ruleOption is required for adopt_rule.';
    if (fields.kind === 'repeal_rule' && fields.ruleNumber === undefined) return 'org_table_motion.ruleNumber is required for repeal_rule.';
    if (fields.kind === 'expel_member' && fields.nomineeHandle === undefined) return 'org_table_motion.nomineeHandle is required for expel_member.';
    if (fields.kind === 'publish_findings') {
      if (fields.title === undefined || fields.summary === undefined || fields.contributionIds === undefined) return 'org_table_motion.title, summary, and contributionIds are required for publish_findings.';
      if (fields.summary.length > summaryRawMax) return `org_table_motion.summary must be at most ${summaryRawMax} characters before it is flattened to one line.`;
      const summary = oneLine(fields.summary);
      if (!summary || summary.length > summaryMax) return `org_table_motion.summary must be 1 to ${summaryMax} characters once it is one line.`;
    }
    if (fields.kind === 'retract_findings' && fields.version === undefined) return 'org_table_motion.version is required for retract_findings.';
  }
  return null;
}

/** Check a validated org action against the heartbeat data's organizations section and its own menu. */
export function orgMenuError(data, kind, value) {
  if (!isOrgActionKind(kind)) return null;
  const snapshot = row(data);
  const section = row(snapshot?.organizations);
  const closed = (reason) => ({ code: 'ACTION_CLOSED', message: `The current menu does not allow ${kind}: ${reason}.`, closedReason: reason });
  if (!section) return closed('visitor_organizations_disabled');
  const menu = row(section.menu);
  if (!Array.isArray(menu?.actions) || !menu.actions.includes(kind)) {
    const reason = row(menu?.closed)?.[kind];
    return closed(typeof reason === 'string' ? reason : 'unavailable');
  }
  const budget = row(row(snapshot?.menu)?.budget);
  if (!(typeof budget?.remaining === 'number' && budget.remaining > 0)) return { code: 'ACTION_BUDGET_EMPTY', message: 'The current menu has no action budget remaining.' };
  const fields = row(value) ?? {};
  const invalid = (message) => ({ code: 'INVALID_ACTION', message });
  const mine = rows(section.mine);
  const organizationId = trimmed(fields.organizationId);
  if (['org_invite', 'org_leave', 'org_say', 'org_vote', 'org_table_motion', 'org_share_link', 'org_contribute'].includes(kind)) {
    const org = mine.find((entry) => entry.organizationId === organizationId);
    if (!org) return invalid("Choose one of the visitor's organizations (organizations.mine).");
    if (kind === 'org_say' && row(org.floor)?.youSpoke === true) return invalid("The visitor already spoke on this organization's floor this window (organizations.mine[].floor.youSpoke).");
    if (kind === 'org_vote') {
      const motion = row(org.openMotion);
      if (!motion || motion.motionId !== trimmed(fields.motionId) || row(motion.voting)?.open !== true || motion.yourBallot !== null) {
        return invalid('Choose the open motion the visitor has not voted on (organizations.mine[].openMotion with voting.open).');
      }
    }
    if (kind === 'org_table_motion') {
      const tabling = row(org.tabling);
      if (tabling?.open !== true || !Array.isArray(tabling.kinds) || !tabling.kinds.includes(fields.kind)) {
        return invalid("Only the window's chair may table a motion, while tabling is open and offers that kind (organizations.mine[].tabling).");
      }
      if (fields.ruleOption !== undefined && !rows(tabling.ruleOptions).some((option) => option.number === fields.ruleOption)) return invalid('Choose a rule option the chair is offered (organizations.mine[].tabling.ruleOptions).');
    }
  }
  if ((kind === 'org_accept' || kind === 'org_decline') && !rows(section.invitations).some((entry) => entry.invitationId === trimmed(fields.invitationId))) {
    return invalid('Choose a pending invitation (organizations.invitations).');
  }
  if (kind === 'org_request_join' && !rows(section.joinable).some((entry) => entry.organizationId === organizationId && entry.requestedToday !== true)) {
    return invalid('Choose an organization from organizations.joinable that the visitor has not asked to join today.');
  }
  if (kind === 'org_admit' && !mine.some((org) => rows(org.joinRequests).some((entry) => entry.requestId === trimmed(fields.requestId)))) {
    return invalid('Choose a pending join request (organizations.mine[].joinRequests).');
  }
  if (kind === 'org_withdraw_contribution' && !mine.some((org) => rows(row(org.findings)?.open).some((entry) => entry.contributionId === trimmed(fields.contributionId) && entry.you === true))) {
    return invalid("Choose one of the visitor's open contributions (organizations.mine[].findings.open where you is true).");
  }
  return null;
}
