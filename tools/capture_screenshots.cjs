#!/usr/bin/env node
/*
 * Regenerate the documentation screenshots from sanitized demo fixtures.
 *
 *   HLC_PLAYWRIGHT=/path/to/playwright node tools/capture_screenshots.cjs
 *
 * The script starts a throwaway console against examples/workspace, intercepts
 * /api/bootstrap, /api/state, /api/repos and /api/system with the JSON under
 * docs/assets/screenshots/demo/, and writes PNGs into docs/assets/screenshots/.
 * Nothing in the fixtures points at a real fleet.
 *
 * Optional env:
 *   HLC_PLAYWRIGHT  path to the playwright module (default: require('playwright'))
 *   HLC_CHROME      path to a Chromium build (default: Playwright's own)
 *   HLC_DEMO_PORT   console port for the capture run (default: 18790)
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

let chromium;
try {
  ({ chromium } = require(process.env.HLC_PLAYWRIGHT || 'playwright'));
} catch (error) {
  console.error('Playwright not found. Set HLC_PLAYWRIGHT to its install path.');
  process.exit(1);
}

const ROOT = path.resolve(__dirname, '..');
const SHOTS = path.join(ROOT, 'docs', 'assets', 'screenshots');
const DEMO = path.join(SHOTS, 'demo');
const PORT = Number(process.env.HLC_DEMO_PORT || 18790);
const BASE = `http://127.0.0.1:${PORT}`;
const TOKEN = 'demo';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const fixtures = {};
for (const name of ['bootstrap', 'state', 'repos', 'system']) {
  fixtures[name] = JSON.parse(fs.readFileSync(path.join(DEMO, `${name}.json`), 'utf8'));
}

// Make the captured data look freshly collected on every run.  Shift the
// relative commit/fetch ages too, so a screenshot taken months after the
// fixture was created still reads "2 days ago" instead of drifting stale.
const now = Math.floor(Date.now() / 1000);
const fixtureBase = Number(fixtures.repos.generated_at) || now;
const shift = Math.max(0, now - fixtureBase);
fixtures.state.generated_at = now;
fixtures.state.age_s = 2;
for (const host of fixtures.state.hosts || []) host.collected_at = now - 2;
for (const row of fixtures.repos.repos || []) {
  (row.gitea || {}).checked_at = now;
  if (row.last_commit?.at) {
    row.last_commit.at += shift;
    row.last_commit.date = new Date(row.last_commit.at * 1000).toISOString().slice(0, 16).replace('T', ' ');
  }
  if (row.last_touch_at) row.last_touch_at += shift;
  if (row.last_fetch_at) row.last_fetch_at += shift;
}
fixtures.repos.generated_at = now;
fixtures.repos.gitea.checked_at = now;
fixtures.repos.connectivity.checked_at = now;
fixtures.state.repos = fixtures.repos;
fixtures.bootstrap.state = fixtures.state;
fixtures.system.app.port = PORT;

async function waitForHealth() {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      const response = await fetch(`${BASE}/api/health`);
      if (response.ok) return;
    } catch (error) {
      /* not up yet */
    }
    await sleep(250);
  }
  throw new Error(`console did not answer on ${BASE}`);
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hlc-shots-'));
  const server = spawn(
    path.join(ROOT, 'hlc'),
    ['serve', '--port', String(PORT), '--no-browser', '--token', TOKEN],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        HLC_REPO: path.join(ROOT, 'examples', 'workspace'),
        HLC_DATA_DIR: path.join(tmp, 'data'),
        HLC_STATE_DIR: path.join(tmp, 'state'),
        HLC_SSH_DIR: path.join(tmp, 'ssh'),
      },
      stdio: 'ignore',
    },
  );

  let browser;
  try {
    await waitForHealth();
    browser = await chromium.launch({ executablePath: process.env.HLC_CHROME || undefined });
    await capture(await newContext(browser, { theme: 'dark', width: 1440, height: 900 }));
    await capture(await newContext(browser, { theme: 'light', width: 1440, height: 900, light: true }));
    await capture(await newContext(browser, { theme: 'dark', width: 390, height: 844, mobile: true }));
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

async function newContext(browser, { theme, width, height, light = false, mobile = false }) {
  fixtures.bootstrap.settings.theme = theme;
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: mobile ? 2 : 1,
  });
  await context.addInitScript((mode) => {
    try {
      localStorage.setItem('hlc-theme', mode);
    } catch (error) {
      /* private mode */
    }
  }, theme);
  await context.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/connectivity/check') || url.pathname.endsWith('/gitea/check')) {
      return route.fulfill({ status: 202, contentType: 'application/json', body: '{"ok":true,"started":false}' });
    }
    const key = url.pathname.startsWith('/api/') ? url.pathname.slice('/api/'.length) : '';
    if (fixtures[key]) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(fixtures[key]),
      });
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => console.error('page error:', error.message));
  return { context, page, light, mobile };
}

async function open(page, hash) {
  await page.goto(`${BASE}/#/${hash}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.sidebar-author', { timeout: 15000 });
  await page.waitForSelector('.view-title', { timeout: 15000 });
  await page.waitForTimeout(650);
}

async function shot(page, file, fullPage = false) {
  await page.screenshot({ path: path.join(SHOTS, file), fullPage });
  console.log(`  ${file}`);
}

async function capture({ context, page, light = false, mobile = false }) {
  try {
    if (mobile) {
      await open(page, 'fleet');
      await shot(page, 'mobile-fleet.png');
      return;
    }

    if (light) {
      await open(page, 'fleet');
      await shot(page, 'fleet-light.png');
      await open(page, 'repo-street');
      await shot(page, 'repo-street-light.png');
      await open(page, 'repo-pulse');
      await shot(page, 'repo-pulse-light.png', true);
      return;
    }

    await open(page, 'fleet');
    await shot(page, 'fleet.png');
    await open(page, 'services');
    await shot(page, 'services.png', true);
    await open(page, 'repo-street');
    await shot(page, 'repo-street.png', true);
    await open(page, 'repo-pulse');
    await shot(page, 'repo-pulse.png', true);

    // Wizard step 1, opened from Hosts.
    await open(page, 'hosts');
    await page.click('[data-add-host]');
    await page.waitForSelector('.modal-overlay', { timeout: 10000 });
    await page.waitForTimeout(400);
    await shot(page, 'onboarding.png');
  } finally {
    await context.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
