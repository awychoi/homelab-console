/*
 * Builds the macOS app icon from app/web/favicon.svg.
 *
 *   node desktop/make_icon.cjs
 *
 * Renders the squircle at each size macOS asks for into build/AppIcon.iconset,
 * then packs desktop/macos/Resources/AppIcon.icns. Needs Playwright and its
 * headless Chromium; set HLC_PLAYWRIGHT or HLC_CHROME if they are not
 * resolvable from node_modules / Playwright's default cache.
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require(process.env.HLC_PLAYWRIGHT || 'playwright');

const EXE = process.env.HLC_CHROME || undefined;
const REPO = path.resolve(__dirname, '..');
const SRC = path.join(REPO, 'app/web/favicon.svg');
const SET = path.join(REPO, 'build/AppIcon.iconset');
const OUT = path.join(REPO, 'desktop/macos/Resources/AppIcon.icns');

const SIZES = [
  ['icon_16x16.png', 16],
  ['icon_16x16@2x.png', 32],
  ['icon_32x32.png', 32],
  ['icon_32x32@2x.png', 64],
  ['icon_128x128.png', 128],
  ['icon_128x128@2x.png', 256],
  ['icon_256x256.png', 256],
  ['icon_256x256@2x.png', 512],
  ['icon_512x512.png', 512],
  ['icon_512x512@2x.png', 1024],
];

(async () => {
  const svg = fs.readFileSync(SRC, 'utf8').replace(/\swidth="\d+"\sheight="\d+"/, '');
  fs.rmSync(SET, { recursive: true, force: true });
  fs.mkdirSync(SET, { recursive: true });

  const browser = await chromium.launch({ executablePath: EXE });
  const page = await browser.newPage({ viewport: { width: 1024, height: 1024 }, deviceScaleFactor: 1 });
  for (const [name, size] of SIZES) {
    await page.setContent(
      `<body style="margin:0;background:transparent">
         <div style="width:${size}px;height:${size}px">${svg.replace('<svg ', `<svg style="width:100%;height:100%;display:block" `)}</div>
       </body>`);
    await page.waitForTimeout(30);
    await page.screenshot({ path: path.join(SET, name), clip: { x: 0, y: 0, width: size, height: size }, omitBackground: true });
  }
  await browser.close();

  execFileSync('/usr/bin/iconutil', ['-c', 'icns', SET, '-o', OUT]);
  const kb = (fs.statSync(OUT).size / 1024).toFixed(0);
  console.log(`wrote ${path.relative(REPO, OUT)} (${kb} KB) from ${path.relative(REPO, SRC)}`);
})();
