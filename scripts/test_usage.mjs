import assert from 'node:assert/strict';
import {stepUsage, usageRows, usageTotals} from '../src/chief/web/usage.js';
const executed = (id, tokens, cost, status = 'completed') => ({status, metadata: {
  execution: {id, model: 'test', usage: {tokens, total_cost_usd: cost}},
}});
const run = {run_id:'r1', step_states:{
  first: executed('e1', {input_tokens:100, output_tokens:30, reasoning_tokens:10, cache_read_tokens:80}, .02),
  loop: {status:'completed', instances:[{instance_id:'i1', step_states:{
    nested: executed('e2', {input_tokens:300, output_tokens:40}, null, 'failed'),
  }}]},
  external: {status:'completed', metadata:{token_usage:{input_tokens:20,output_tokens:10}}},
  missing: {status:'completed'},
  pending: {status:'pending'},
}};
const replay = {run_id:'r2', step_states:{first:run.step_states.first}};
const rows=usageRows([run,replay],{steps:[{id:'nested',goal:'Largest step'}]});
assert.equal(rows.length,4);
assert.equal(rows[0].path,'loop/i1/nested');
assert.equal(rows[0].title,'Largest step');
const totals=usageTotals(rows);
assert.equal(totals.tokens.total_tokens,500);
assert.equal(totals.tokens.input_tokens,420);
assert.equal(totals.tokens.output_tokens,80);
assert.equal(totals.tokens.reasoning_tokens,10);
assert.equal(totals.coverage.reasoning_tokens,1);
assert.equal(totals.coverage.total_tokens,3);
assert.equal(totals.cost,.02);
assert.equal(totals.costCoverage,1);
assert.equal(stepUsage(executed('live',{input_tokens:20},null,'running')).provisional,true);
assert.deepEqual(stepUsage(executed('invalid',{input_tokens:-1,output_tokens:'3'},NaN)).tokens,{});
assert.equal(usageTotals([]).tokens.total_tokens,undefined);
console.log('PASS: normalized usage, nested steps, external reports, replay deduplication, ranking and partial coverage');
