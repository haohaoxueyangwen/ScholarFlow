/**
 * Session-scoped execution audit for T5.2.
 * Survives before_agent_start; cleared only on session_start.
 */

/** @typedef {object} SessionExecutionAuditEntry
 * @property {number} seq
 * @property {number} turnId
 * @property {string} timestamp
 * @property {string} kind
 * @property {string} toolName
 * @property {boolean} blocked
 * @property {string} [reason]
 * @property {Record<string, unknown>} [originalParams]
 * @property {Record<string, unknown>} [executedParams]
 * @property {string} [action]
 * @property {object} [projectContext]
 * @property {Record<string, unknown>} [bridgeArguments]
 */

/** @type {SessionExecutionAuditEntry[]} */
let entries = [];
let turnId = 0;
let seq = 0;

export function clearSessionExecutionAudit() {
  entries = [];
  turnId = 0;
  seq = 0;
}

export function beginExecutionAuditTurn() {
  turnId += 1;
  seq += 1;
  entries.push({
    seq,
    turnId,
    timestamp: new Date().toISOString(),
    kind: "turn_start",
    toolName: "",
    blocked: false,
    reason: `execution audit turn ${turnId} started`,
    action: "pass",
  });
  return turnId;
}

/**
 * @param {Omit<SessionExecutionAuditEntry, "seq"|"turnId"|"timestamp">} entry
 */
export function appendSessionExecutionAudit(entry) {
  seq += 1;
  entries.push({
    seq,
    turnId,
    timestamp: new Date().toISOString(),
    ...entry,
  });
}

export function getSessionExecutionAudit() {
  return entries.map((entry) => ({ ...entry }));
}

/**
 * @param {string} [toolName]
 */
export function getBlockedExecutionAudit(toolName) {
  return getSessionExecutionAudit().filter(
    (entry) => entry.blocked && (!toolName || entry.toolName === toolName),
  );
}
