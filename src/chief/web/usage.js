/* Shared accounting for list totals and the on-demand usage drawer. */
export const TOKEN_FIELDS = ['input_tokens', 'output_tokens', 'reasoning_tokens',
  'cache_read_tokens', 'cache_write_tokens', 'total_tokens'];
const valid = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;

export function stepUsage(step) {
  const execution = step.metadata?.execution;
  const report = execution?.usage || {};
  // External harnesses can report the same normalized shape through existing metadata.
  const raw = report.tokens || step.metadata?.token_usage || {};
  const tokens = Object.fromEntries(TOKEN_FIELDS.filter((key) => Number.isSafeInteger(raw[key]) && raw[key] >= 0)
    .map((key) => [key, raw[key]]));
  if (tokens.input_tokens != null && tokens.output_tokens != null)
    tokens.total_tokens = tokens.input_tokens + tokens.output_tokens;
  const cost = report.total_cost_usd ?? raw.cost_usd;
  return { tokens, cost: valid(cost) ? cost : null,
    provisional: !!report.provisional || step.status === 'running', scope: report.scope,
    model: execution?.model || step.metadata?.model || '',
  };
}

export function usageRows(runs, definition = null) {
  const rows = new Map();
  const steps = new Map((definition?.steps || []).map((step) => [step.id, step]));
  const walk = (run, states, prefix = []) => {
    for (const [id, step] of Object.entries(states || {})) {
      const path = [...prefix, id];
      const usage = stepUsage(step);
      const execution = step.metadata?.execution;
      const attempted = (!steps.has(id) || steps.get(id).type === 'task') && !step.instances && ['running', 'completed', 'failed', 'blocked'].includes(step.status);
      if (execution || Object.keys(usage.tokens).length || attempted) {
        // Replayed runs can carry an earlier execution's evidence; count that id once.
        const key = execution?.id || `${run.run_id}:${path.join('/')}`;
        const row = { ...usage, key, runId: run.run_id, path: path.join('/'),
          title: steps.get(id)?.goal || id, status: step.status };
        const old = rows.get(key);
        if (!old || (old.provisional && !row.provisional)
            || (row.tokens.total_tokens ?? -1) > (old.tokens.total_tokens ?? -1)) rows.set(key, row);
      }
      for (const instance of step.instances || []) walk(run, instance.step_states, [...path, instance.instance_id]);
    }
  };
  for (const run of runs || []) walk(run, run.step_states);
  return [...rows.values()].sort((a, b) => (b.tokens.total_tokens ?? -1) - (a.tokens.total_tokens ?? -1));
}

export function usageTotals(rows) {
  const coverage = {};
  const tokens = {};
  for (const key of TOKEN_FIELDS) {
    const known = rows.filter((row) => row.tokens[key] != null);
    coverage[key] = known.length;
    if (known.length) tokens[key] = known.reduce((sum, row) => sum + row.tokens[key], 0);
  }
  const costs = rows.filter((row) => row.cost != null);
  return { tokens, coverage, count: rows.length,
    cost: costs.length ? costs.reduce((sum, row) => sum + row.cost, 0) : null,
    costCoverage: costs.length, provisional: rows.some((row) => row.provisional) };
}
