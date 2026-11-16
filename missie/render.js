const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const b = await chromium.launch();
  const url = 'file://' + path.resolve(__dirname, 'plaat.html');
  for (const [formaat, w, h] of [['liggend', 1920, 1080], ['staand', 1080, 1350]])
    for (const thema of ['donker', 'licht']) {
      const pg = await b.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 2 });
      await pg.goto(`${url}?thema=${thema}&formaat=${formaat}`, { waitUntil: 'networkidle' });
      await pg.evaluate(() => document.fonts.ready);
      await pg.locator('#plaat').screenshot({ path: path.join(__dirname, `missie-${formaat}-${thema}.png`) });
      await pg.close();
    }
  await b.close();
})();
