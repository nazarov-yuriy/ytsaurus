// "Play" session: drive ytsaurus-ui through the interactions an Iceberg viewer needs,
// recording browser->UI-server traffic to a HAR while the mock records
// UI-server->proxy traffic to proxy-traffic.jsonl (MOCK_RECORD).
//
// Run: NODE_PATH=<ui>/node_modules node play.js

const {chromium} = require('playwright');
const path = require('path');

const BASE = 'http://localhost:8080';
const OUT = __dirname;

async function step(name, fn) {
  process.stdout.write(`== ${name} ... `);
  try {
    await fn();
    console.log('ok');
  } catch (e) {
    console.log(`FAILED: ${String(e.message || e).split('\n')[0]}`);
  }
}

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: {width: 1500, height: 900},
    recordHar: {path: path.join(OUT, 'browser-traffic.har'), content: 'embed'},
  });
  const page = await context.newPage();
  const settle = async (ms = 2500) => {
    await page.waitForLoadState('networkidle', {timeout: 30000}).catch(() => {});
    await page.waitForTimeout(ms);
  };

  await step('root listing /', async () => {
    await page.goto(`${BASE}/mock/navigation?path=/`, {timeout: 60000});
    await settle();
  });

  await step('click into home', async () => {
    await page.getByRole('link', {name: 'home', exact: true}).first().click();
    await settle();
  });

  await step('navigate to warehouse', async () => {
    await page.goto(`${BASE}/mock/navigation?path=//home/iceberg/warehouse`);
    await settle();
  });

  await step('open trips table (content)', async () => {
    await page.getByRole('link', {name: 'trips', exact: true}).first().click();
    await settle(4000);
  });

  for (const tab of ['Schema', 'Attributes', 'User attributes', 'ACL']) {
    await step(`trips tab: ${tab}`, async () => {
      await page.getByText(tab, {exact: true}).first().click();
      await settle(1500);
    });
  }

  await step('trips back to Content', async () => {
    await page.getByText('Content', {exact: true}).first().click();
    await settle(2000);
  });

  await step('paging: offset 50 via URL', async () => {
    await page.goto(`${BASE}/mock/navigation?path=//home/iceberg/warehouse/trips&offsetMode=row&offsetValue=50`);
    await settle(3000);
  });

  await step('paging: click next-page control', async () => {
    const next = page.locator('button[title*="ext"], .table-pagination button, [class*="pagination"] button').last();
    await next.click({timeout: 5000});
    await settle(2000);
  });

  await step('open events table (any-typed column)', async () => {
    await page.goto(`${BASE}/mock/navigation?path=//home/iceberg/warehouse/events`);
    await settle(3500);
  });

  await step('nonexistent path (error flow)', async () => {
    await page.goto(`${BASE}/mock/navigation?path=//home/iceberg/nope`);
    await settle(2000);
  });

  await step('map node attributes tab', async () => {
    await page.goto(`${BASE}/mock/navigation?path=//home/iceberg/warehouse&navmode=attributes`);
    await settle(1500);
  });

  await context.close(); // flushes HAR
  await browser.close();
  console.log('done; HAR written to browser-traffic.har');
})();
