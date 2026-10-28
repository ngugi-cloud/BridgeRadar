// Offline tests for the pure logic in apps-script/Code.gs.   Run:  node --test apps-script/tests/logic.test.js
// (Also run automatically by `python -m unittest discover -s tests -t .` when Node is installed.)
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const g = require(path.join(__dirname, '..', 'Code.gs'));

const fmt = (d) => d.toISOString().slice(0, 10);
const Q = g.TABS.find((t) => t.name === 'Priority_Queue');
const JOBS = g.TABS.find((t) => t.name === 'Active_Job_Leads');
const CONTACTS = g.TABS.find((t) => t.name === 'HR_Contacts');
const LOG = g.TABS.find((t) => t.name === 'Scrape_Log');
const DEF = {Owner: 'Head of BD', Status: 'New'};

test('parseCsv handles quotes, commas, newlines, CRLF and BOM', () => {
  const text = '﻿a,b,c\r\n1,"x, y","say ""hi"""\r\n2,"line1\nline2",\r\n';
  assert.deepEqual(g.parseCsv(text), [['a', 'b', 'c'], ['1', 'x, y', 'say "hi"'], ['2', 'line1\nline2', '']]);
  assert.deepEqual(g.parseCsv(''), []);
});

test('toCsv round-trips through parseCsv', () => {
  const rows = [['Lead_ID', 'Notes'], ['J-1', 'called, left "msg"\nfollow up'], ['J-2', '']];
  assert.deepEqual(g.parseCsv(g.toCsv(rows)), rows);
});

test('safeCell neutralises formula-like text', () => {
  assert.equal(g.safeCell('=HYPERLINK("x")'), '\'=HYPERLINK("x")');
  assert.equal(g.safeCell('+254 20 123'), "'+254 20 123");
  assert.equal(g.safeCell('Karen'), 'Karen');
  assert.equal(g.safeCell(null), '');
});

test('isTouched: owner alone is not "worked on"', () => {
  assert.equal(g.isTouched({Owner: 'Ngugi', Status: 'New'}), false);
  assert.equal(g.isTouched({Status: 'Contacted'}), true);
  assert.equal(g.isTouched({Status: 'New', Notes: 'x'}), true);
  assert.equal(g.isTouched({Last_contacted: new Date()}), true);
});

function csv(header, rows) {
  return {header, rows: rows.map((r) => Object.fromEntries(header.map((h, i) => [h, r[i] ?? ''])))};
}

test('mergeTab keeps sheet human columns and fills defaults for new rows', () => {
  const c = csv(['job_id', 'title'], [['a1', 'Nurse'], ['b2', 'Driver']]);
  const ex = {header: ['job_id', 'title', 'Owner', 'Status', 'Last_contacted', 'Notes', 'Sync_State'],
    values: [['a1', 'OLD TITLE', 'Ngugi', 'Contacted', new Date('2026-10-20'), 'called', 'current']]};
  const m = g.mergeTab(JOBS, c, ex, '2026-10-23', DEF);
  assert.deepEqual(m.header, ['job_id', 'title', 'Owner', 'Status', 'Last_contacted', 'Notes', 'Sync_State']);
  assert.equal(m.values[0][1], 'Nurse');                       // pipeline column refreshed
  assert.deepEqual(m.values[0].slice(2, 6), ['Ngugi', 'Contacted', new Date('2026-10-20'), 'called']);
  assert.deepEqual(m.values[1].slice(2, 6), ['Head of BD', 'New', '', '']);
  assert.equal(m.stats.added, 1);
});

test('mergeTab: Priority_Queue human columns stay in the agreed position; sheet beats CSV', () => {
  const header = ['Lead_ID', 'Company', 'Owner', 'Status', 'Last_contacted', 'Notes', 'Outcome', 'Score'];
  const c = csv(header, [['J-1', 'Karen', 'Head of BD', 'New', '', '', '', '65'],
    ['J-2', 'Twiga', 'Ngugi', 'Contacted', '2026-10-01', 'from pipeline', '', '50']]);
  const ex = {header: header.concat(['Sync_State']), values: [['J-1', 'Karen', 'Ngugi', 'Meeting booked', '', 'met CEO', '', '60', 'current']]};
  const m = g.mergeTab(Q, c, ex, '2026-10-23', DEF);
  assert.deepEqual(m.header, header.concat(['Sync_State']));
  assert.deepEqual(m.values[0].slice(2, 7), ['Ngugi', 'Meeting booked', '', 'met CEO', '']);
  assert.equal(m.values[0][7], '65');
  assert.deepEqual(m.values[1].slice(2, 6), ['Ngugi', 'Contacted', '2026-10-01', 'from pipeline']);   // new to sheet: CSV carried values
});

test('mergeTab lifecycle for rows that leave the pipeline data', () => {
  const header = ['job_id', 'title', 'Owner', 'Status', 'Last_contacted', 'Notes', 'Sync_State'];
  const ex = {header, values: [
    ['t1', 'Touched', 'Ngugi', 'Contacted', '', '', 'current'],
    ['u1', 'Untouched', 'Ngugi', 'New', '', '', 'current'],
    ['o1', 'Old touched', 'Ngugi', 'New', '', 'note', 'removed 2026-01-01']]};
  let m = g.mergeTab(JOBS, csv(['job_id', 'title'], []), ex, '2026-10-23', DEF);
  assert.deepEqual(m.values.map((r) => [r[0], r[6]]), [['t1', 'removed 2026-10-23']]);
  assert.deepEqual([m.stats.keptRemoved, m.stats.dropped], [1, 2]);
  // the removal date does not move on later runs
  m = g.mergeTab(JOBS, csv(['job_id', 'title'], []), {header, values: m.values}, '2026-11-30', DEF);
  assert.equal(m.values[0][6], 'removed 2026-10-23');
  // and the row comes back to "current" if the pipeline sees it again
  m = g.mergeTab(JOBS, csv(['job_id', 'title'], [['t1', 'Touched']]), {header, values: m.values}, '2026-12-01', DEF);
  assert.deepEqual([m.values[0][3], m.values[0][6]], ['Contacted', 'current']);
});

test('mergeTab never keeps purged contacts (privacy)', () => {
  const header = ['contact_id', 'email', 'Owner', 'Status', 'Last_contacted', 'Notes'];
  const ex = {header, values: [['k1', 'jane@karen.co.ke', 'Ngugi', 'Contacted', '', 'spoke']]};
  const m = g.mergeTab(CONTACTS, csv(['contact_id', 'email'], []), ex, '2026-10-23', DEF);
  assert.equal(m.values.length, 0);
  assert.equal(m.header.includes('Sync_State'), false);
});

test('mergeTab: formula injection guarded in pipeline columns only; duplicate keys; scrape log newest first + cap', () => {
  const c = csv(['job_id', 'title'], [['a', '=IMPORTXML("http://x")'], ['a', 'dup']]);
  const ex = {header: ['job_id', 'title', 'Owner', 'Status', 'Last_contacted', 'Notes'], values: [['a', 'x', '', 'New', '', '=1+1']]};
  const m = g.mergeTab(JOBS, c, ex, '2026-10-23', DEF);
  assert.equal(m.values.length, 1);
  assert.equal(m.values[0][1], '\'=IMPORTXML("http://x")');
  assert.equal(m.values[0][5], '=1+1');                          // your own cell is left as you typed it
  const log = csv(['run_date', 'engine'], Array.from({length: 5}, (_, i) => [`2026-10-0${i + 1}`, 'e']));
  const lm = g.mergeTab({...LOG, maxRows: 3}, log, null, '2026-10-23', DEF);
  assert.deepEqual(lm.values.map((r) => r[0]), ['2026-10-05', '2026-10-04', '2026-10-03']);
});

test('mergeTab tolerates an apostrophe-prefixed key read back from the sheet', () => {
  const ex = {header: ['job_id', 'title', 'Owner', 'Status', 'Last_contacted', 'Notes'], values: [["'=k", 't', 'N', 'Won', '', '']]};
  const m = g.mergeTab(JOBS, csv(['job_id', 'title'], [['=k', 't']]), ex, '2026-10-23', DEF);
  assert.equal(m.values[0][3], 'Won');
});

test('checkFreshness', () => {
  assert.equal(g.checkFreshness(null, null, '2026-10-23').fresh, false);
  assert.match(g.checkFreshness({run_date: '2026-10-22'}, null, '2026-10-23').reason, /2026-10-22/);
  assert.equal(g.checkFreshness({run_date: '2026-10-23'}, {run_date: '2026-10-22'}, '2026-10-23').fresh, false);
  const ok = g.checkFreshness({run_date: '2026-10-23', failed_steps: ['engine1_jobs']}, {run_date: '2026-10-23'}, '2026-10-23');
  assert.deepEqual([ok.fresh, ok.failed], [true, ['engine1_jobs']]);
});

test('buildDigest: only new Hot leads, escaped, null when nothing to say', () => {
  const rows = [
    {Company: 'Karen <b>Hospital</b>', Temperature: 'Hot', Hot_Since: '2026-10-23', Score: '70', Source_URL: 'https://n/1', Flags: ''},
    {Company: 'Old Hot', Temperature: 'Hot', Hot_Since: '2026-10-10', Flags: 'Hot and untouched 7+ days'},
    {Company: 'Bad link', Temperature: 'Hot', Hot_Since: '2026-10-23', Source_URL: 'javascript:alert(1)'},
    {Company: 'Warm', Temperature: 'Warm', Hot_Since: ''}];
  const d = g.buildDigest(rows, '2026-10-23', 'https://sheet', []);
  assert.match(d.subject, /2 new Hot leads/);
  assert.ok(d.html.includes('Karen &lt;b&gt;Hospital&lt;/b&gt;'));
  assert.ok(!d.html.includes('javascript:'));
  assert.ok(d.html.includes('still marked New after 7+ days'));
  assert.ok(d.html.includes('Signal-based prioritization'));
  assert.equal(g.buildDigest([rows[3]], '2026-10-23', 'u', []), null);
  assert.match(g.buildDigest([], '2026-10-23', 'u', ['engine4_signals']).subject, /pipeline warning/);
});

test('leadStatusRows: Sheet -> lead_status.csv contract', () => {
  const header = ['Lead_ID', 'Company', 'Owner', 'Status', 'Last_contacted', 'Notes', 'Outcome', 'Sync_State'];
  const rows = g.leadStatusRows(header, [['J-1', 'K', 'Ngugi', 'Won', new Date('2026-10-21T00:00:00Z'), ' note ', 'signed', 'current'],
    ['', 'blank id', '', '', '', '', '', '']], fmt);
  assert.deepEqual(rows, [g.LEAD_STATUS_HEADER, ['J-1', 'Ngugi', 'Won', '2026-10-21', 'note', 'signed']]);
  assert.deepEqual(g.leadStatusRows(['x'], [['y']], fmt), [g.LEAD_STATUS_HEADER]);
});

test('ownedRows and safeToReplace', () => {
  const sup = g.OWNED.find((o) => o.name === 'Suppression');
  const rows = g.ownedRows(sup, sup.header, [['EMAIL', 'a@b.co', 'asked', new Date('2026-10-01T00:00:00Z')],
    ['domain', '', 'incomplete', ''], ['', '', '', '']], fmt);
  assert.deepEqual(rows, [sup.header, ['email', 'a@b.co', 'asked', '2026-10-01']]);
  assert.equal(g.safeToReplace('type,value\nemail,a@b.co\n', [sup.header]), false);   // would wipe the list
  assert.equal(g.safeToReplace('type,value\n', [sup.header]), true);
  assert.equal(g.safeToReplace(null, rows), true);
});

test('Status choices match the pipeline', () => {
  assert.deepEqual(g.STATUSES, ['New', 'Contacted', 'Meeting booked', 'Won', 'Lost']);
});
