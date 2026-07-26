// Headless-Chromium check against the composed UI (run via the `e2e` service).
const {chromium} = require('playwright');

const UI = (process.env.UI_URL || 'http://ui').replace(/\/$/, '');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width: 1500, height: 900}});
  const errors = [];
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) errors.push(`http ${r.status()}: ${r.url()}`); });

  await page.goto(`${UI}/mock/navigation?path=//home/iceberg/warehouse`,
                  {waitUntil: 'networkidle', timeout: 60000});
  await page.waitForTimeout(2500);
  const nav = await page.locator('body').innerText();
  if (!nav.includes('trips') || !nav.includes('events')) throw new Error('navigation listing incomplete');

  await page.goto(`${UI}/mock/navigation?path=//home/iceberg/warehouse/trips`,
                  {waitUntil: 'networkidle', timeout: 60000});
  await page.waitForTimeout(3500);
  const table = await page.locator('body').innerText();
  if (!table.includes('trip_id') || !table.includes('Amsterdam')) throw new Error('table rows missing');

  if (errors.length) throw new Error('browser errors: ' + errors.slice(0, 5).join(' | '));
  console.log('E2E OK: navigation and table render with zero errors');
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
