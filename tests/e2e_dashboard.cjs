// End-to-end: serve docs/, route the Apps Script URL to the local harness, load the dashboard
// in Chromium, check for errors (incl. CSP violations) and take screenshots.
//   node tests/e2e_dashboard.cjs <harnessPort> <seed.json> <outDir>
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const [harnessPort, seedFile, outDir] = process.argv.slice(2);
const seed = JSON.parse(fs.readFileSync(seedFile, 'utf8'));
const docs = path.join(__dirname, '..', 'docs');
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  const p = path.join(docs, decodeURIComponent(req.url.split('?')[0]).replace(/^\/labtracker/, '') || '/');
  const f = p.endsWith('/') ? path.join(p, 'index.html') : p;
  if (!f.startsWith(docs) || !fs.existsSync(f)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': types[path.extname(f)] || 'application/octet-stream' });
  fs.createReadStream(f).pipe(res);
});

(async () => {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}/labtracker/`;
  const execUrl = 'https://script.google.com/macros/s/AKfycbTESTdeployment1234567890/exec';
  const frag = new URLSearchParams({ u: execUrl, k: seed.share, t: seed.token, id: seed.user, n: 'Alex Morgan' });
  const browser = await chromium.launch();
  const problems = [];
  const replies = [];
  const shoot = async (name, opts, actions) => {
    const ctx = await browser.newContext(opts);
    const page = await ctx.newPage();
    page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') problems.push(`${name}: ${m.text()}`); });
    page.on('pageerror', (e) => problems.push(`${name}: ${e.message}`));
    await page.route('https://script.google.com/**', async (route) => {
      const r = await fetch(`http://127.0.0.1:${harnessPort}/exec`, { method: 'POST', body: route.request().postData() });
      const text = await r.text();
      replies.push(JSON.parse(text));
      await route.fulfill({ status: 200, contentType: 'application/json', body: text,
                            headers: { 'Access-Control-Allow-Origin': '*' } });
    });
    await page.goto(`${base}#${frag}`);
    await page.waitForSelector('#main:not([hidden])', { timeout: 10000 });
    if (actions) await actions(page);
    await page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: true });
    const text = await page.locator('body').innerText();
    await ctx.close();
    return text;
  };
  const desk = { viewport: { width: 1100, height: 800 }, deviceScaleFactor: 1 };
  const day = await shoot('day-light', desk);
  // A second poll with nothing new must come back "unchanged" and leave the page intact.
  replies.length = 0;
  const polled = await shoot('poll-unchanged', desk, async (p) => {
    await p.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await p.waitForTimeout(800);
  });
  const pollReplies = replies.slice();
  const week = await shoot('week-light', desk, async (p) => { await p.click('[data-view=week]'); await p.click('#prev'); });
  await shoot('month-dark', { ...desk, colorScheme: 'dark' }, async (p) => { await p.click('[data-view=month]'); });
  await shoot('phone-day', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, isMobile: true });
  const empty = await (async () => {
    const ctx = await browser.newContext(desk); const page = await ctx.newPage();
    await page.goto(base); const t = await page.locator('#notice').innerText(); await ctx.close(); return t;
  })();

  const checks = [
    [/Away from desk/.test(day), 'status shows away'],
    [/Clocked in|continued from previous day/.test(day), 'day timeline lists the shift start'],
    [/adjusted/.test(week), 'week view shows the adjusted marker'],
    [/needs a private link/.test(empty), 'no-link notice'],
    [pollReplies.length >= 2 && pollReplies[0].rows && pollReplies.at(-1).unchanged === true && /Away from desk/.test(polled),
     'second poll is "unchanged" (no sheet read) and the page keeps its data'],
    [problems.length === 0, `no console errors: ${problems.join(' | ')}`],
  ];
  await browser.close();
  server.close();
  let failed = 0;
  for (const [ok, label] of checks) { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) failed++; }
  process.exit(failed ? 1 : 0);
})();
