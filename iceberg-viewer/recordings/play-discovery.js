// Discovery session: visit the UI pages the original play session skipped, so
// the mock records every proxy call the real UI makes there (MOCK_RECORD must
// point at discovery-traffic.jsonl). Errors are expected — the point is to
// observe which endpoints get called, not to make the pages work.
//
// Run: NODE_PATH=../ytsaurus-ui/packages/ui/node_modules node play-discovery.js

const {chromium} = require('playwright');

const BASE = process.env.UI_URL || 'http://localhost:8080';
const PAGES = [
  '/mock/navigation?path=/',
  '/mock/navigation?path=//tmp',
  '/mock/queries',
  '/mock/operations',
  '/mock/accounts',
  '/mock/scheduling',
  '/mock/tablet_cell_bundles',
  '/mock/system',
  '/mock/components/versions',
  '/mock/users',
  '/mock/groups',
  '/mock/path-viewer',
  '/mock/navigation?path=//home/iceberg/warehouse/trips&navmode=acl',
  '/mock/navigation?path=//home/iceberg/warehouse/trips&navmode=locks',
];

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width: 1500, height: 900}});
  for (const path of PAGES) {
    process.stdout.write(`== ${path} ... `);
    try {
      await page.goto(`${BASE}${path}`, {waitUntil: 'networkidle', timeout: 45000});
      await page.waitForTimeout(2000);
      console.log('visited');
    } catch (e) {
      console.log(`visited (with errors: ${String(e.message).split('\n')[0].slice(0, 60)})`);
    }
  }
  await browser.close();
  console.log('discovery session done');
})();
