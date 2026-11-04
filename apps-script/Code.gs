/**
 * @OnlyCurrentDoc
 * (Google permission: this one Sheet only, not all your Sheets)
 *
 * Bridge Lead Radar - Google Sheet sync (Apps Script)
 * ---------------------------------------------------------------------------
 * What it does
 *   1. dailyImport  (~07:00 EAT)  reads the pipeline's CSV files from GitHub and
 *      upserts them into tabs, keyed by stable IDs. Your columns (Owner, Status,
 *      Last_contacted, Notes, Outcome) are NEVER overwritten.
 *   2. writeBack    (~03:00 EAT, before the pipeline runs) sends your edits back
 *      to the PRIVATE repo: Priority_Queue statuses -> lead_status.csv,
 *      Suppression tab -> suppression.csv, Clients tab -> clients.csv.
 *   3. A daily email digest of NEW Hot leads (only when there is something to say).
 *
 * Safety rules built into the code
 *   * Writes go ONLY to the repo named in PRIVATE_REPO, and only if GitHub says
 *     that repo is private.
 *   * If the data is stale (pipeline did not run today), it retries every 30 min
 *     up to 4 times, then emails you instead of importing old data.
 *   * A backup of every human-entered column is made before each import
 *     (hidden tabs Backup_YYYY-MM-DD, last 7 kept).
 *   * It will not replace a non-empty suppression/clients file with an empty one.
 *
 * Setup: see apps-script/SETUP.md (click by click).
 * Label: signal-based prioritization. Scores are heuristics, not predictions.
 */

// ============================================================================
// Configuration (code side). Your settings live in Script Properties.
// ============================================================================
var TZ = 'Africa/Nairobi';
var STATUSES = ['New', 'Contacted', 'Meeting booked', 'Won', 'Lost'];
var STD_HUMAN = ['Owner', 'Status', 'Last_contacted', 'Notes'];
var QUEUE_HUMAN = ['Owner', 'Status', 'Last_contacted', 'Notes', 'Outcome'];
var IMPORT_HOUR = 7;            // EAT; Google runs it some time in that hour
var WRITEBACK_HOUR = 3;         // EAT; the GitHub workflow runs at ~05:00 EAT
var RETRY_MINUTES = 30;
var MAX_ATTEMPTS = 4;
var KEEP_BACKUPS = 7;
var KEEP_REMOVED_DAYS = 180;
var SCRAPE_LOG_MAX_ROWS = 3000;
var PROTECT_TAG = 'BLR: pipeline columns - edits here are overwritten at the next import';

// Each managed tab. repo: 'public' or 'private'. key: stable ID column.
// keepRemoved: keep rows you worked on after they leave the pipeline data.
var TABS = [
  {name: 'Priority_Queue', repo: 'private', file: 'priority_queue.csv', key: 'Lead_ID', human: QUEUE_HUMAN, keepRemoved: true},
  {name: 'Active_Job_Leads', repo: 'public', file: 'active_jobs.csv', key: 'job_id', human: STD_HUMAN, keepRemoved: true},
  {name: 'Hiring_Signals', repo: 'private', file: 'hiring_signals.csv', key: 'signal_id', human: STD_HUMAN, keepRemoved: true},
  {name: 'HR_Contacts', repo: 'private', file: 'hr_contacts.csv', key: 'contact_id', human: STD_HUMAN, keepRemoved: false},  // privacy: purged rows stay purged
  {name: 'Kenya_Companies_Master', repo: 'public', file: 'kenya_companies_master.csv', key: 'company_id', human: STD_HUMAN, keepRemoved: true},
  {name: 'Advisory_Flags', repo: 'private', file: 'advisory_flags.csv', key: 'advisory_id', human: ['Notes'], keepRemoved: false},  // layoffs etc.: info only, never scored
  {name: 'Signal_Candidates', repo: 'public', file: 'signal_company_candidates.csv', key: 'source_url', human: ['Notes'], keepRemoved: false},
  {name: 'Manual_Sources', repo: 'public', file: 'manual_integration_required.csv', key: 'source_id', human: ['Notes'], keepRemoved: false},
  {name: 'Manual_Contact_Check', repo: 'private', file: 'manual_contact_check.csv', key: 'company_id', human: ['Notes'], keepRemoved: false},
  {name: 'Scrape_Log', repo: 'public', file: 'scrape_log.csv', key: '', human: [], keepRemoved: false, newestFirst: true, maxRows: SCRAPE_LOG_MAX_ROWS}
];
// Owner-maintained tabs. They flow BACK to the private repo.
var OWNED = [
  {name: 'Suppression', file: 'suppression.csv', header: ['type', 'value', 'reason', 'added_on'],
   types: ['company_id', 'name', 'domain', 'email']},
  {name: 'Clients', file: 'clients.csv', header: ['name', 'company_id', 'since', 'notes']}
];
var LEAD_STATUS_FILE = 'lead_status.csv';
var LEAD_STATUS_HEADER = ['Lead_ID'].concat(QUEUE_HUMAN);

// ============================================================================
// PURE LOGIC (no Google services; unit-tested in Node: apps-script/tests)
// ============================================================================

/** RFC 4180 CSV -> array of rows. Handles quotes, "" escapes, commas and newlines in fields, BOM, CRLF. */
function parseCsv(text) {
  text = String(text || '');
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  var rows = [], row = [], field = '', i = 0, inQ = false, n = text.length;
  while (i < n) {
    var c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQ = false; i++; continue;
      }
      field += c; i++; continue;
    }
    if (c === '"') { inQ = true; i++; continue; }
    if (c === ',') { row.push(field); field = ''; i++; continue; }
    if (c === '\r') { i++; continue; }
    if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; i++; continue; }
    field += c; i++;
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows.filter(function (r) { return !(r.length === 1 && r[0] === ''); });
}

/** CSV text -> {header: [...], rows: [{col: value}]} */
function csvToObjects(text) {
  var arr = parseCsv(text);
  if (!arr.length) return {header: [], rows: []};
  var header = arr[0];
  var rows = arr.slice(1).map(function (r) {
    var o = {};
    header.forEach(function (h, j) { o[h] = r[j] !== undefined ? r[j] : ''; });
    return o;
  });
  return {header: header, rows: rows};
}

/** array of rows -> CSV text (quoted only when needed, \n line ends like Python's csv module output read back). */
function toCsv(rows) {
  return rows.map(function (r) {
    return r.map(function (v) {
      var s = v === null || v === undefined ? '' : String(v);
      return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }).join(',');
  }).join('\n') + '\n';
}

/** Text that Sheets would treat as a formula is stored as plain text (CSV/formula injection guard). */
function safeCell(v) {
  var s = v === null || v === undefined ? '' : String(v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function isBlank(v) {
  return v === null || v === undefined || String(v).trim() === '';
}

/** A row counts as "worked on" when you changed status or wrote something. Owner alone does not count. */
function isTouched(human) {
  var st = String(human.Status || '').trim();
  return (st !== '' && st !== 'New') || !isBlank(human.Notes) || !isBlank(human.Last_contacted) || !isBlank(human.Outcome);
}

/**
 * Merge pipeline rows into what is in the sheet.
 *   spec        tab spec (key, human, keepRemoved, newestFirst, maxRows)
 *   csv         {header, rows}
 *   existing    {header: [...], values: [[...]]} as read from the sheet (may be empty)
 *   todayStr    'YYYY-MM-DD'
 *   defaults    {Owner: '...', Status: 'New'}
 * Returns {header, values, stats}. Human values in the sheet always win.
 */
function mergeTab(spec, csv, existing, todayStr, defaults) {
  var human = spec.human || [];
  var header = csv.header.slice();
  human.forEach(function (h) { if (header.indexOf(h) < 0) header.push(h); });
  if (spec.key && spec.keepRemoved && header.indexOf('Sync_State') < 0) header.push('Sync_State');
  var stats = {imported: csv.rows.length, added: 0, keptRemoved: 0, dropped: 0};

  var rows = csv.rows.slice();
  if (spec.newestFirst) rows.reverse();
  if (spec.maxRows && rows.length > spec.maxRows) rows = rows.slice(0, spec.maxRows);

  // index existing sheet rows by key
  var exH = existing && existing.header ? existing.header : [];
  var exByKey = {}, exOrder = [];
  if (spec.key && exH.indexOf(spec.key) >= 0) {
    var ki = exH.indexOf(spec.key);
    (existing.values || []).forEach(function (r) {
      var k = String(r[ki] === undefined ? '' : r[ki]).replace(/^'/, '');
      if (k === '' || exByKey[k]) return;
      var o = {};
      exH.forEach(function (h, j) { o[h] = r[j]; });
      exByKey[k] = o;
      exOrder.push(k);
    });
  }

  var out = [], seen = {};
  rows.forEach(function (r) {
    var key = spec.key ? String(r[spec.key] || '') : '';
    var ex = key ? exByKey[key] : null;
    if (key) {
      if (seen[key]) return;          // duplicate key in the CSV: first wins
      seen[key] = true;
    }
    if (key && !ex) stats.added++;
    out.push(header.map(function (h) {
      if (h === 'Sync_State') return 'current';
      if (human.indexOf(h) >= 0) {
        if (ex && h in ex) return ex[h];                                   // the sheet wins
        if (!isBlank(r[h])) return r[h];                                   // new row: value the pipeline carried
        return defaults && defaults[h] !== undefined ? defaults[h] : '';
      }
      return safeCell(r[h]);
    }));
  });

  // rows the pipeline no longer has
  exOrder.forEach(function (k) {
    if (seen[k]) return;
    var ex = exByKey[k];
    var h = {};
    human.forEach(function (c) { h[c] = ex[c]; });
    var state = String(ex.Sync_State || '');
    var since = /^removed (\d{4}-\d{2}-\d{2})$/.exec(state);
    var removedOn = since ? since[1] : todayStr;
    if (spec.keepRemoved && isTouched(h) && daysBetween(removedOn, todayStr) <= KEEP_REMOVED_DAYS) {
      stats.keptRemoved++;
      out.push(header.map(function (c) {
        if (c === 'Sync_State') return 'removed ' + removedOn;
        return ex[c] === undefined ? '' : (human.indexOf(c) >= 0 ? ex[c] : safeCell(ex[c]));
      }));
    } else {
      stats.dropped++;
    }
  });
  return {header: header, values: out, stats: stats};
}

function daysBetween(a, b) {
  var da = Date.UTC(+a.slice(0, 4), +a.slice(5, 7) - 1, +a.slice(8, 10));
  var db = Date.UTC(+b.slice(0, 4), +b.slice(5, 7) - 1, +b.slice(8, 10));
  return Math.round((db - da) / 86400000);
}

/** Is the pipeline output from today? Both meta.json copies must agree. */
function checkFreshness(privMeta, pubMeta, todayStr) {
  if (!privMeta) return {fresh: false, reason: 'private meta.json not found (has the workflow run yet?)'};
  if (privMeta.run_date !== todayStr) return {fresh: false, reason: 'pipeline last ran on ' + privMeta.run_date + ', not today'};
  if (pubMeta && pubMeta.run_date !== privMeta.run_date)
    return {fresh: false, reason: 'public and private data are from different runs (' + pubMeta.run_date + ' vs ' + privMeta.run_date + ')'};
  return {fresh: true, reason: '', failed: privMeta.failed_steps || []};
}

/** Hot leads that became Hot on this run. */
function newHotLeads(queueRows, runDate) {
  return queueRows.filter(function (r) { return r.Temperature === 'Hot' && r.Hot_Since === runDate; });
}

function untouchedHot(queueRows) {
  return queueRows.filter(function (r) { return r.Temperature === 'Hot' && /untouched/.test(r.Flags || ''); });
}

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** Returns {subject, html} or null when there is nothing worth emailing. */
function buildDigest(queueRows, runDate, sheetUrl, failedSteps) {
  var fresh = newHotLeads(queueRows, runDate);
  var stale = untouchedHot(queueRows);
  failedSteps = failedSteps || [];
  if (!fresh.length && !stale.length && !failedSteps.length) return null;
  var allHot = queueRows.filter(function (r) { return r.Temperature === 'Hot'; }).length;
  var rows = fresh.map(function (r) {
    return '<tr><td><b>' + esc(r.Company) + '</b><br><small>' + esc(r.Sector) + '</small></td>' +
      '<td>' + esc(r.Score) + '</td><td>' + esc(r.Trigger_Type) + '</td><td>' + esc(r.Suggested_Angle) +
      (r.Flags ? '<br><small>Flags: ' + esc(r.Flags) + '</small>' : '') + '</td>' +
      '<td>' + (/^https?:\/\//.test(r.Source_URL || '') ? '<a href="' + esc(r.Source_URL) + '">source</a>' : '') + '</td></tr>';
  }).join('');
  var html = '<div style="font-family:Arial,sans-serif;font-size:14px">' +
    '<h2 style="margin:0 0 8px">Bridge Lead Radar - ' + esc(runDate) + '</h2>' +
    '<p>' + fresh.length + ' new Hot lead(s). ' + allHot + ' Hot in total. Action: outreach within 48 hours.</p>' +
    (fresh.length ? '<table cellpadding="6" style="border-collapse:collapse" border="1">' +
      '<tr><th>Company</th><th>Score</th><th>Trigger</th><th>Suggested angle</th><th></th></tr>' + rows + '</table>' : '') +
    (stale.length ? '<p><b>' + stale.length + ' Hot lead(s) still marked New after 7+ days:</b> ' +
      stale.map(function (r) { return esc(r.Company); }).join(', ') + '</p>' : '') +
    (failedSteps.length ? '<p style="color:#b00"><b>Pipeline warning:</b> these steps failed today: ' +
      esc(failedSteps.join(', ')) + '. Data from those steps is from the previous run.</p>' : '') +
    '<p><a href="' + esc(sheetUrl) + '">Open the Sheet</a></p>' +
    '<p style="color:#666;font-size:12px">Signal-based prioritization: scores are heuristics from public data, ' +
    'not predictions. Check each source before outreach.</p></div>';
  var subject = 'Bridge Lead Radar: ' + fresh.length + ' new Hot lead' + (fresh.length === 1 ? '' : 's') +
    (failedSteps.length ? ' (pipeline warning)' : '') + ' - ' + runDate;
  return {subject: subject, html: html};
}

/** Turn a sheet cell into CSV text. Dates become YYYY-MM-DD. fmt(date) is injected (Utilities in Apps Script). */
function cellText(v, fmt) {
  if (v instanceof Date) return fmt(v);
  if (v === null || v === undefined) return '';
  return String(v).replace(/^'/, '').trim();
}

/** Priority_Queue tab -> lead_status.csv rows (header included). */
function leadStatusRows(header, values, fmt) {
  var idx = LEAD_STATUS_HEADER.map(function (h) { return header.indexOf(h); });
  if (idx[0] < 0) return [LEAD_STATUS_HEADER];
  var out = [LEAD_STATUS_HEADER];
  values.forEach(function (r) {
    var id = cellText(r[idx[0]], fmt);
    if (!id) return;
    out.push(idx.map(function (i) { return i < 0 ? '' : cellText(r[i], fmt); }));
  });
  return out;
}

/** Owner-maintained tab -> CSV rows (header included). Blank rows dropped; suppression type lower-cased. */
function ownedRows(spec, header, values, fmt) {
  var idx = spec.header.map(function (h) { return header.indexOf(h); });
  var out = [spec.header];
  values.forEach(function (r) {
    var row = idx.map(function (i) { return i < 0 ? '' : cellText(r[i], fmt); });
    if (spec.types) row[0] = row[0].toLowerCase();
    var keyFilled = spec.types ? (row[0] && row[1]) : (row[0] || row[1]);
    if (keyFilled) out.push(row);
  });
  return out;
}

/** Refuse to replace a non-empty file with an empty one (protects against an accidentally cleared tab). */
function safeToReplace(oldText, newRows) {
  var oldCount = Math.max(0, parseCsv(oldText || '').length - 1);
  var newCount = Math.max(0, newRows.length - 1);
  return !(oldCount > 0 && newCount === 0);
}

// ============================================================================
// GOOGLE / GITHUB SIDE
// ============================================================================
function props_() { return PropertiesService.getScriptProperties(); }

function setting_(name, fallback) {
  var v = props_().getProperty(name);
  return isBlank(v) ? (fallback === undefined ? '' : fallback) : String(v).trim();
}

function today_() { return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd'); }
function fmtDate_(d) { return Utilities.formatDate(d, TZ, 'yyyy-MM-dd'); }

function repoName_(which) { return which === 'private' ? setting_('PRIVATE_REPO') : setting_('PUBLIC_REPO'); }

function repoPath_(which, file) {
  var base = which === 'private' ? setting_('PRIVATE_DATA_PATH', '') : setting_('PUBLIC_DATA_PATH', 'data');
  base = base.replace(/^\/+|\/+$/g, '');
  return (base ? base + '/' : '') + file;
}

function gh_(method, path, accept, payload) {
  var token = setting_('GITHUB_TOKEN');
  if (!token) throw new Error('GITHUB_TOKEN is not set. Run Bridge Radar > First-time setup.');
  var opts = {method: method, muteHttpExceptions: true,
    headers: {Authorization: 'Bearer ' + token, Accept: accept || 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'}};
  if (payload) { opts.contentType = 'application/json'; opts.payload = JSON.stringify(payload); }
  var res = UrlFetchApp.fetch('https://api.github.com' + path, opts);
  return {code: res.getResponseCode(), text: res.getContentText('UTF-8')};
}

function contentsUrl_(repo, path) {
  return '/repos/' + encodeURIComponent(setting_('GITHUB_OWNER')) + '/' + encodeURIComponent(repo) +
    '/contents/' + path.split('/').map(encodeURIComponent).join('/');
}

/** File text, or null if it does not exist yet. */
function readFile_(which, file) {
  var r = gh_('get', contentsUrl_(repoName_(which), repoPath_(which, file)) + '?ref=' +
    encodeURIComponent(setting_('BRANCH', 'main')), 'application/vnd.github.raw');
  if (r.code === 404) return null;
  if (r.code !== 200) throw new Error('GitHub read ' + which + '/' + file + ' failed: HTTP ' + r.code + ' ' + r.text.slice(0, 200));
  return r.text;
}

var privateChecked_ = false;
function assertPrivateRepo_() {
  if (privateChecked_) return;
  var repo = setting_('PRIVATE_REPO');
  if (!repo || repo === setting_('PUBLIC_REPO')) throw new Error('PRIVATE_REPO must be set and differ from PUBLIC_REPO.');
  var r = gh_('get', '/repos/' + encodeURIComponent(setting_('GITHUB_OWNER')) + '/' + encodeURIComponent(repo));
  if (r.code !== 200) throw new Error('Cannot see the private repo (HTTP ' + r.code + '). Check GITHUB_OWNER, PRIVATE_REPO and the token.');
  if (JSON.parse(r.text).private !== true) throw new Error('STOPPED: ' + repo + ' is not private. Nothing was written.');
  privateChecked_ = true;
}

/** Write a file to the PRIVATE repo only. Returns true if a commit was made. */
function writePrivateFile_(file, text, message) {
  assertPrivateRepo_();
  var repo = setting_('PRIVATE_REPO'), path = repoPath_('private', file), branch = setting_('BRANCH', 'main');
  for (var attempt = 0; attempt < 2; attempt++) {
    var cur = gh_('get', contentsUrl_(repo, path) + '?ref=' + encodeURIComponent(branch));
    var sha = null;
    if (cur.code === 200) {
      var j = JSON.parse(cur.text);
      sha = j.sha;
      if (j.content && Utilities.newBlob(Utilities.base64Decode(j.content.replace(/\n/g, ''))).getDataAsString('UTF-8') === text) return false;
    } else if (cur.code !== 404) {
      throw new Error('GitHub read ' + path + ' failed: HTTP ' + cur.code);
    }
    var body = {message: message, content: Utilities.base64Encode(text, Utilities.Charset.UTF_8), branch: branch};
    if (sha) body.sha = sha;
    var put = gh_('put', contentsUrl_(repo, path), null, body);
    if (put.code === 200 || put.code === 201) return true;
    if (put.code !== 409) throw new Error('GitHub write ' + path + ' failed: HTTP ' + put.code + ' ' + put.text.slice(0, 200));
  }
  throw new Error('GitHub write ' + path + ' kept conflicting; try again later.');
}

// ---------------------------------------------------------------- sheet helpers
function sheet_(name) {
  var ss = SpreadsheetApp.getActive();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

function readSheet_(sh) {
  var lr = sh.getLastRow(), lc = sh.getLastColumn();
  if (lr < 1 || lc < 1) return {header: [], values: []};
  var all = sh.getRange(1, 1, lr, lc).getValues();
  return {header: all[0].map(String), values: all.slice(1).filter(function (r) {
    return r.some(function (v) { return !isBlank(v); });
  })};
}

function writeSheet_(sh, spec, header, values) {
  sh.clearContents();
  sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold').setBackground('#e8eef7');
  sh.setFrozenRows(1);
  var human = spec.human || [];
  if (values.length) {
    // pipeline columns as plain text so IDs like 12e45 never turn into numbers
    header.forEach(function (h, j) {
      var col = sh.getRange(2, j + 1, values.length, 1);
      if (h === 'Last_contacted') col.setNumberFormat('yyyy-mm-dd');
      else if (human.indexOf(h) < 0) col.setNumberFormat('@');
    });
    sh.getRange(2, 1, values.length, header.length).setValues(values);
  }
  var extra = sh.getMaxRows() - Math.max(values.length + 1, 2);
  if (extra > 500) sh.deleteRows(Math.max(values.length + 2, 3), extra - 100);   // keep the sheet small
  decorate_(sh, spec, header, Math.max(values.length, 1));
}

function decorate_(sh, spec, header, nRows) {
  var human = spec.human || [];
  sh.getProtections(SpreadsheetApp.ProtectionType.RANGE).forEach(function (p) {
    if ((p.getDescription() || '').indexOf('BLR:') === 0) p.remove();
  });
  // warning-only protection on each block of pipeline columns
  var start = -1;
  for (var j = 0; j <= header.length; j++) {
    var machine = j < header.length && human.indexOf(header[j]) < 0;
    if (machine && start < 0) start = j;
    if (!machine && start >= 0) {
      sh.getRange(1, start + 1, nRows + 1, j - start).protect().setDescription(PROTECT_TAG).setWarningOnly(true);
      start = -1;
    }
  }
  var si = header.indexOf('Status');
  if (si >= 0 && human.indexOf('Status') >= 0) {
    sh.getRange(2, si + 1, nRows, 1).setDataValidation(SpreadsheetApp.newDataValidation()
      .requireValueInList(STATUSES, true).setAllowInvalid(false).build());
  }
  var li = header.indexOf('Last_contacted');
  if (li >= 0) {
    sh.getRange(2, li + 1, nRows, 1).setDataValidation(SpreadsheetApp.newDataValidation()
      .requireDate().setAllowInvalid(false).setHelpText('A date, e.g. 2026-10-23').build());
  }
  human.forEach(function (h) {
    var i = header.indexOf(h);
    if (i >= 0) sh.getRange(1, i + 1).setBackground('#fff2cc');   // yellow header = your column
  });
  if (spec.name === 'Priority_Queue') {
    var ti = header.indexOf('Temperature');
    if (ti >= 0) {
      var rng = sh.getRange(2, ti + 1, nRows, 1);
      var rule = function (txt, bg) {
        return SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(txt).setBackground(bg).setRanges([rng]).build();
      };
      sh.setConditionalFormatRules([rule('Hot', '#f4c7c3'), rule('Warm', '#fce8b2'), rule('Cold', '#c9daf8'),
        rule('Expired', '#eeeeee'), rule('Closed', '#d9ead3')]);
    }
  }
}

function setupOwnedTab_(spec) {
  var sh = sheet_(spec.name);
  var cur = readSheet_(sh);
  if (!cur.header.length) {
    var text = null;
    try { text = readFile_('private', spec.file); } catch (e) { /* first run: repo may be empty */ }
    var csv = csvToObjects(text || '');
    var vals = csv.rows.map(function (r) { return spec.header.map(function (h) { return safeCell(r[h] || ''); }); });
    sh.getRange(1, 1, 1, spec.header.length).setValues([spec.header]).setFontWeight('bold').setBackground('#fff2cc');
    sh.setFrozenRows(1);
    if (vals.length) sh.getRange(2, 1, vals.length, spec.header.length).setNumberFormat('@').setValues(vals);
  }
  if (spec.types) {
    sh.getRange(2, 1, Math.max(sh.getMaxRows() - 1, 1), 1).setDataValidation(SpreadsheetApp.newDataValidation()
      .requireValueInList(spec.types, true).setAllowInvalid(false).build());
  }
}

// ---------------------------------------------------------------- backup
function backup_() {
  var ss = SpreadsheetApp.getActive();
  var name = 'Backup_' + today_();
  if (ss.getSheetByName(name)) return;
  var out = [['Tab', 'Key', 'Owner', 'Status', 'Last_contacted', 'Notes', 'Outcome', 'Row_JSON']];
  TABS.forEach(function (spec) {
    if (!spec.key || !spec.human.length) return;
    var sh = ss.getSheetByName(spec.name);
    if (!sh) return;
    var cur = readSheet_(sh), ki = cur.header.indexOf(spec.key);
    if (ki < 0) return;
    cur.values.forEach(function (r) {
      var h = {};
      spec.human.forEach(function (c) { var i = cur.header.indexOf(c); h[c] = i < 0 ? '' : r[i]; });
      if (isTouched(h) || !isBlank(h.Owner)) {
        out.push([spec.name, r[ki], h.Owner || '', h.Status || '', h.Last_contacted || '', h.Notes || '', h.Outcome || '', '']);
      }
    });
  });
  OWNED.forEach(function (spec) {
    var sh = ss.getSheetByName(spec.name);
    if (!sh) return;
    var cur = readSheet_(sh);
    cur.values.forEach(function (r) { out.push([spec.name, '', '', '', '', '', '', JSON.stringify(r)]); });
  });
  var b = ss.insertSheet(name);
  b.getRange(1, 1, out.length, out[0].length).setNumberFormat('@').setValues(out.map(function (r) {
    return r.map(function (v) { return v instanceof Date ? fmtDate_(v) : safeCell(v); });
  }));
  b.hideSheet();
  ss.getSheets().filter(function (s) { return /^Backup_\d{4}-\d{2}-\d{2}$/.test(s.getName()); })
    .sort(function (a, c) { return a.getName() < c.getName() ? 1 : -1; })
    .slice(KEEP_BACKUPS).forEach(function (s) { ss.deleteSheet(s); });
}

// ============================================================================
// ENTRY POINTS (triggers and menu)
// ============================================================================
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Bridge Radar')
    .addItem('1. First-time setup', 'setup')
    .addSeparator()
    .addItem('Import latest data now', 'importNow')
    .addItem('Check data connection', 'checkConnectionNow')
    .addItem('Send my edits to the pipeline now', 'writeBackNow')
    .addItem('Suppress the selected row (do not contact)', 'suppressSelected')
    .addItem('Mark selected company as a client', 'clientSelected')
    .addSeparator()
    .addItem('Send me a test digest', 'testDigest')
    .addToUi();
}

/** Menu: asks for any missing settings, checks GitHub access, builds tabs, installs the two daily triggers. */
function setup() {
  var ui = SpreadsheetApp.getUi();
  var ask = [
    ['GITHUB_OWNER', 'Your GitHub username (the owner of both repos):', ''],
    ['PUBLIC_REPO', 'Public code repo name:', 'BridgeRadar'],
    ['PRIVATE_REPO', 'PRIVATE data repo name:', 'bridge-lead-data'],
    ['GITHUB_TOKEN', 'Paste the fine-grained GitHub token (only the private repo, Contents: Read and write):', ''],
    ['DEFAULT_OWNER', 'Name to put in the Owner column for new leads:', 'Head of BD']
  ];
  for (var i = 0; i < ask.length; i++) {
    var k = ask[i][0];
    if (!isBlank(setting_(k))) continue;
    var r = ui.prompt('Bridge Radar setup', ask[i][1] + (ask[i][2] ? '\n(Leave blank for: ' + ask[i][2] + ')' : ''), ui.ButtonSet.OK_CANCEL);
    if (r.getSelectedButton() !== ui.Button.OK) { ui.alert('Setup cancelled. Nothing changed.'); return; }
    var v = r.getResponseText().trim() || ask[i][2];
    if (!v) { ui.alert(k + ' is required. Run setup again.'); return; }
    props_().setProperty(k, v);
  }
  try {
    assertPrivateRepo_();
    var pub = gh_('get', '/repos/' + encodeURIComponent(setting_('GITHUB_OWNER')) + '/' + encodeURIComponent(setting_('PUBLIC_REPO')));
    if (pub.code !== 200) throw new Error('Cannot see the public repo ' + setting_('PUBLIC_REPO') + ' (HTTP ' + pub.code + ').');
  } catch (e) {
    ui.alert('Setup stopped: ' + e.message + '\n\nTo change a setting: Extensions > Apps Script > Project Settings > Script Properties.');
    return;
  }
  SpreadsheetApp.getActive().setSpreadsheetTimeZone(TZ);
  OWNED.forEach(setupOwnedTab_);
  installTriggers_();
  var msg = 'Setup done.\n\nDaily: edits sent back ~0' + WRITEBACK_HOUR + ':00, data imported ~0' + IMPORT_HOUR +
    ':00 (Nairobi time).\n\nImport now?';
  if (ui.alert('Bridge Radar', msg, ui.ButtonSet.YES_NO) === ui.Button.YES) importNow();
}

function installTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (['dailyImport', 'retryImport', 'writeBack'].indexOf(t.getHandlerFunction()) >= 0) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('dailyImport').timeBased().atHour(IMPORT_HOUR).everyDays(1).inTimezone(TZ).create();
  ScriptApp.newTrigger('writeBack').timeBased().atHour(WRITEBACK_HOUR).everyDays(1).inTimezone(TZ).create();
}

function importNow() {
  var res = runImport_({force: true, digest: false});
  SpreadsheetApp.getUi().alert('Bridge Radar', res.message, SpreadsheetApp.getUi().ButtonSet.OK);
}

/** Menu: diagnose Sheet-to-GitHub settings and data freshness without importing anything. */
function checkConnectionNow() {
  var ui = SpreadsheetApp.getUi(), lines = [];
  try {
    assertPrivateRepo_();
    lines.push('Private repository: OK (' + setting_('GITHUB_OWNER') + '/' + setting_('PRIVATE_REPO') + ')');

    var pub = gh_('get', '/repos/' + encodeURIComponent(setting_('GITHUB_OWNER')) + '/' +
      encodeURIComponent(setting_('PUBLIC_REPO')));
    lines.push('Public repository: ' + (pub.code === 200 ? 'OK' : 'FAILED (HTTP ' + pub.code + ')'));

    var privMeta = JSON.parse(readFile_('private', 'meta.json') || 'null');
    var pubMeta = JSON.parse(readFile_('public', 'meta.json') || 'null');
    lines.push('Private run date: ' + (privMeta && privMeta.run_date ? privMeta.run_date : 'missing'));
    lines.push('Public run date: ' + (pubMeta && pubMeta.run_date ? pubMeta.run_date : 'missing'));

    var today = today_();
    var fresh = checkFreshness(privMeta, pubMeta, today);
    lines.push('Today (' + today + '): ' + (fresh.fresh ? 'fresh' : 'stale - ' + fresh.reason));

    var files = [];
    TABS.forEach(function (spec) {
      var text = readFile_(spec.repo, spec.file);
      if (text !== null) files.push(spec.name);
    });
    lines.push('Data tabs readable: ' + (files.length ? files.join(', ') : 'none'));
  } catch (e) {
    lines.push('Connection check failed: ' + e.message);
  }
  ui.alert('Bridge Radar connection check', lines.join('\n'), ui.ButtonSet.OK);
}

/** Time trigger, ~07:00 EAT. */
function dailyImport() { runImport_({force: false, digest: true}); }
function retryImport() { runImport_({force: false, digest: true}); }

function runImport_(opt) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return {message: 'Another import is running. Try again in a minute.'};
  try {
    ScriptApp.getProjectTriggers().forEach(function (t) {
      if (t.getHandlerFunction() === 'retryImport') ScriptApp.deleteTrigger(t);
    });
    var today = today_();
    props_().getKeys().forEach(function (k) {      // forget retry counters from earlier days
      if (k.indexOf('ATTEMPTS_') === 0 && k !== 'ATTEMPTS_' + today) props_().deleteProperty(k);
    });
    var privMeta = JSON.parse(readFile_('private', 'meta.json') || 'null');
    var pubMeta = JSON.parse(readFile_('public', 'meta.json') || 'null');
    var fr = checkFreshness(privMeta, pubMeta, today);
    if (!fr.fresh && !opt.force) {
      var key = 'ATTEMPTS_' + today, n = Number(props_().getProperty(key) || 0) + 1;
      props_().setProperty(key, String(n));
      if (n < MAX_ATTEMPTS) {
        ScriptApp.newTrigger('retryImport').timeBased().after(RETRY_MINUTES * 60000).create();
        return {message: 'Data not fresh (' + fr.reason + '). Retrying in ' + RETRY_MINUTES + ' minutes.'};
      }
      notify_('Bridge Radar: no fresh data today', '<p>The pipeline data is stale: ' + esc(fr.reason) +
        '.</p><p>The Sheet was NOT updated. Check the GitHub Actions run (Actions tab of ' +
        esc(setting_('PUBLIC_REPO')) + '), then use Bridge Radar &gt; Import latest data now.</p>');
      return {message: 'Gave up after ' + n + ' attempts: ' + fr.reason};
    }
    backup_();
    var defaults = {Owner: setting_('DEFAULT_OWNER', 'Head of BD'), Status: 'New'};
    var summary = [];
    TABS.forEach(function (spec) {
      var text = readFile_(spec.repo, spec.file);
      if (text === null) { summary.push(spec.name + ': no file yet'); return; }
      var sh = sheet_(spec.name);
      var m = mergeTab(spec, csvToObjects(text), readSheet_(sh), today, defaults);
      writeSheet_(sh, spec, m.header, m.values);
      summary.push(spec.name + ': ' + m.values.length + ' rows (' + m.stats.added + ' new, ' + m.stats.keptRemoved + ' kept)');
    });
    OWNED.forEach(setupOwnedTab_);
    var runDate = privMeta ? privMeta.run_date : today;
    props_().setProperty('LAST_IMPORT', new Date().toISOString() + ' run_date=' + runDate);
    if (opt.digest && props_().getProperty('LAST_DIGEST') !== runDate) {
      var q = csvToObjects(readFile_('private', 'priority_queue.csv') || '').rows;
      var d = buildDigest(q, runDate, SpreadsheetApp.getActive().getUrl(), fr.failed || []);
      if (d) notify_(d.subject, d.html);
      props_().setProperty('LAST_DIGEST', runDate);
    }
    return {message: (fr.fresh ? '' : 'Note: ' + fr.reason + '.\n\n') + summary.join('\n')};
  } finally {
    lock.releaseLock();
  }
}

/** Time trigger, ~03:00 EAT (before the pipeline). */
function writeBack() { return runWriteBack_(); }

function writeBackNow() {
  var msg;
  try { msg = runWriteBack_(); } catch (e) { msg = 'Stopped: ' + e.message; }
  SpreadsheetApp.getUi().alert('Bridge Radar', msg, SpreadsheetApp.getUi().ButtonSet.OK);
}

function runWriteBack_() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return 'Another job is running. Try again in a minute.';
  try {
    assertPrivateRepo_();
    var ss = SpreadsheetApp.getActive(), done = [], skipped = [];
    var q = ss.getSheetByName('Priority_Queue');
    if (q) {
      var cur = readSheet_(q);
      var rows = leadStatusRows(cur.header, cur.values, fmtDate_);
      if (rows.length > 1 && writePrivateFile_(LEAD_STATUS_FILE, toCsv(rows), 'Sheet write-back: lead status')) done.push('lead statuses');
    }
    OWNED.forEach(function (spec) {
      var sh = ss.getSheetByName(spec.name);
      if (!sh) return;
      var cur = readSheet_(sh);
      var rows = ownedRows(spec, cur.header, cur.values, fmtDate_);
      var old = readFile_('private', spec.file);
      if (!safeToReplace(old, rows)) {
        skipped.push(spec.name + ' (tab is empty but the file is not: not overwritten)');
        return;
      }
      if (writePrivateFile_(spec.file, toCsv(rows), 'Sheet write-back: ' + spec.name)) done.push(spec.name);
    });
    if (skipped.length) {
      notify_('Bridge Radar: write-back skipped', '<p>Not sent, to protect your data:</p><p>' +
        skipped.map(esc).join('<br>') + '</p><p>If you really meant to empty it, delete the file in the private repo on GitHub.</p>');
    }
    return (done.length ? 'Sent: ' + done.join(', ') : 'Nothing changed since the last send.') +
      (skipped.length ? '\nSkipped: ' + skipped.join('; ') : '');
  } finally {
    lock.releaseLock();
  }
}

/** Menu: add the selected row's contact email (HR_Contacts) or company to Suppression. */
function suppressSelected() {
  var ui = SpreadsheetApp.getUi(), sh = SpreadsheetApp.getActiveSheet(), cur = readSheet_(sh);
  var row = SpreadsheetApp.getActiveRange().getRow();
  if (row < 2) { ui.alert('Select a data row first.'); return; }
  var r = sh.getRange(row, 1, 1, cur.header.length).getValues()[0];
  var get = function (h) { var i = cur.header.indexOf(h); return i < 0 ? '' : String(r[i]).replace(/^'/, '').trim(); };
  var entry = get('email') ? ['email', get('email').toLowerCase()]
    : get('company_id') ? ['company_id', get('company_id')]
    : get('Company_ID') ? ['company_id', get('Company_ID')]
    : get('company') ? ['name', get('company')] : get('Company') ? ['name', get('Company')] : null;
  if (!entry) { ui.alert('Could not find an email, company_id or company name in this row.'); return; }
  var reason = ui.prompt('Suppress ' + entry[0] + ' "' + entry[1] + '"', 'Reason (e.g. asked not to be contacted):', ui.ButtonSet.OK_CANCEL);
  if (reason.getSelectedButton() !== ui.Button.OK) return;
  var s = sheet_('Suppression');
  if (!readSheet_(s).header.length) setupOwnedTab_(OWNED[0]);
  s.appendRow([entry[0], safeCell(entry[1]), safeCell(reason.getResponseText()), today_()]);
  ui.alert('Added to Suppression. It reaches the pipeline at the next write-back (~03:00), or use "Send my edits to the pipeline now".');
}

/** Menu: add the selected row's company to Clients. */
function clientSelected() {
  var ui = SpreadsheetApp.getUi(), sh = SpreadsheetApp.getActiveSheet(), cur = readSheet_(sh);
  var row = SpreadsheetApp.getActiveRange().getRow();
  if (row < 2) { ui.alert('Select a data row first.'); return; }
  var r = sh.getRange(row, 1, 1, cur.header.length).getValues()[0];
  var get = function (h) { var i = cur.header.indexOf(h); return i < 0 ? '' : String(r[i]).replace(/^'/, '').trim(); };
  var name = get('Company') || get('display_name') || get('company');
  var cid = get('Company_ID') || get('company_id');
  if (!name && !cid) { ui.alert('Could not find a company in this row.'); return; }
  var s = sheet_('Clients');
  if (!readSheet_(s).header.length) setupOwnedTab_(OWNED[1]);
  s.appendRow([safeCell(name), safeCell(cid), today_(), '']);
  ui.alert('Added ' + (name || cid) + ' to Clients. Its leads stop after the next write-back and pipeline run.');
}

function testDigest() {
  var q = csvToObjects(readFile_('private', 'priority_queue.csv') || '').rows;
  var meta = JSON.parse(readFile_('private', 'meta.json') || 'null');
  var runDate = meta ? meta.run_date : today_();
  var d = buildDigest(q, runDate, SpreadsheetApp.getActive().getUrl(), meta ? meta.failed_steps : []) ||
    {subject: 'Bridge Lead Radar: test (no new Hot leads on ' + runDate + ')', html: '<p>The digest works. No new Hot leads today.</p>'};
  notify_(d.subject, d.html);
  SpreadsheetApp.getUi().alert('Sent to ' + digestTo_());
}

function digestTo_() { return setting_('DIGEST_TO', Session.getEffectiveUser().getEmail()); }

function notify_(subject, html) {
  MailApp.sendEmail({to: digestTo_(), subject: subject, htmlBody: html, name: 'Bridge Lead Radar'});
}

// Node test hook (ignored by Apps Script, where `module` does not exist)
if (typeof module !== 'undefined') {
  module.exports = {parseCsv: parseCsv, csvToObjects: csvToObjects, toCsv: toCsv, safeCell: safeCell, isTouched: isTouched,
    mergeTab: mergeTab, checkFreshness: checkFreshness, newHotLeads: newHotLeads, buildDigest: buildDigest,
    leadStatusRows: leadStatusRows, ownedRows: ownedRows, safeToReplace: safeToReplace, daysBetween: daysBetween,
    TABS: TABS, OWNED: OWNED, LEAD_STATUS_HEADER: LEAD_STATUS_HEADER, STATUSES: STATUSES};
}
