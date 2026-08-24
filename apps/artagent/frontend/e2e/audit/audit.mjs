/* Temporary UI/UX audit harness — not a test file (excluded from Playwright testMatch).
 * Usage: node e2e/audit/audit.mjs <loopTag> [surfaceFilter]
 *   loopTag e.g. "loop00-before" or "loop03-after"
 * Screenshots + metrics land in <repo>/artifacts/ui-eval/.
 */
import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const OUT = path.resolve(fileURLToPath(import.meta.url), '../../../../../../artifacts/ui-eval');
const BASE = process.env.AUDIT_BASE_URL || 'http://localhost:5173';
const loopTag = process.argv[2] || 'loop00-before';
const filter = process.argv[3] || '';

const SURFACES = [
  { slug: 'landing-default', url: '/', tab: null },
  { slug: 'landing-campaign', url: '/go/recall-growth?utm_source=ig&utm_campaign=eval', tab: null },
  { slug: 'app-inbox', url: '/app', tab: 'Inbox' },
  { slug: 'app-phone', url: '/app', tab: 'Phone' },
  { slug: 'app-outbox', url: '/app', tab: 'Outbox' },
  { slug: 'app-campaigns', url: '/app', tab: 'Campaigns' },
  { slug: 'app-sent', url: '/app', tab: 'Sent' },
  { slug: 'app-incidents', url: '/app', tab: 'Incidents' },
  { slug: 'app-roi', url: '/app', tab: 'ROI' },
  { slug: 'app-settings', url: '/app', tab: 'Settings' },
];
const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
];

const MEASURE = () => {
  const cs = (el) => getComputedStyle(el);
  const parseC = (c) => { const m = c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/); return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null; };
  const effBg = (el) => { const chain = []; let n = el; while (n && n !== document.documentElement) { chain.unshift(n); n = n.parentElement; } let acc = [30, 30, 30, 1]; for (const node of chain) { const c = parseC(cs(node).backgroundColor); if (c && c[3] > 0) { const a = c[3]; acc = [c[0] * a + acc[0] * (1 - a), c[1] * a + acc[1] * (1 - a), c[2] * a + acc[2] * (1 - a), 1]; } } return acc; };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const ratio = (fg, bg) => { const l1 = lum(fg), l2 = lum(bg); return +(((Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)).toFixed(2)); };
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const info = (el) => { if (!el || !vis(el)) return null; const s = cs(el); const r = el.getBoundingClientRect(); const fg = parseC(s.color); return { fs: s.fontSize, lh: s.lineHeight, contrast: fg ? ratio(fg, effBg(el)) : null, box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] }; };

  const out = {};
  out.hScroll = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  out.h1s = [...document.querySelectorAll('h1')].filter(vis).length;
  out.headings = [...document.querySelectorAll('h1,h2,h3')].filter(vis).slice(0, 12).map((h) => ({ tag: h.tagName, text: h.textContent.trim().slice(0, 40), ...info(h) }));

  // Interactive target sizes (visible only)
  out.smallTargets = [...document.querySelectorAll('button, a, select, input, textarea, [role="tab"]')]
    .filter(vis)
    .map((el) => { const r = el.getBoundingClientRect(); return { t: (el.getAttribute('aria-label') || el.textContent || el.name || el.type || '').trim().slice(0, 30), w: Math.round(r.width), h: Math.round(r.height) }; })
    .filter((x) => x.w < 44 || x.h < 40);

  // Text overflow / truncation
  out.overflowing = [...document.querySelectorAll('main *, .cr-surface *')]
    .filter((el) => el.children.length === 0 && !['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) && vis(el) && el.scrollWidth > el.clientWidth + 2)
    .slice(0, 10)
    .map((el) => ({ cls: el.className && String(el.className).slice(0, 40), text: el.textContent.trim().slice(0, 40), sw: el.scrollWidth, cw: el.clientWidth }));

  // Low contrast text nodes (< 4.5)
  const seen = new Set();
  out.lowContrast = [...document.querySelectorAll('p, span, small, label, a, button, h1, h2, h3, strong, dt, dd, output, td, th, li')]
    .filter((el) => vis(el) && el.textContent.trim().length > 2)
    .map((el) => { const s = cs(el); const fg = parseC(s.color); if (!fg) return null; const r = ratio(fg, effBg(el)); const px = parseFloat(s.fontSize); const bold = parseInt(s.fontWeight, 10) >= 700; const large = px >= 24 || (px >= 18.66 && bold); const min = large ? 3 : 4.5; if (r >= min) return null; const key = `${String(el.className).slice(0, 30)}|${r}`; if (seen.has(key)) return null; seen.add(key); return { cls: String(el.className).slice(0, 40), text: el.textContent.trim().slice(0, 32), ratio: r, fs: s.fontSize, min }; })
    .filter(Boolean).slice(0, 14);

  // Surface-specific probes
  const tabs = document.querySelector('.cr-tabs');
  if (tabs) {
    const kids = [...tabs.children].filter(vis);
    const rows = new Set(kids.map((k) => Math.round(k.getBoundingClientRect().y)));
    out.tabBar = { count: kids.length, rows: rows.size, cols: cs(tabs).gridTemplateColumns.split(' ').length, boxes: kids.map((k) => { const r = k.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }) };
  }
  const pills = [...document.querySelectorAll('.cr-status-pill, .cr-priority-pill, .cr-campaign-pill')].filter(vis);
  out.rawEnumPills = pills.map((p) => p.textContent.trim()).filter((t) => t.includes('_')).slice(0, 10);
  const grid = document.querySelector('[data-testid="incident-form"]');
  if (grid) {
    const s = cs(grid);
    out.incidentForm = { cols: s.gridTemplateColumns.split(' ').length, boxes: [...grid.children].filter(vis).map((c) => { const r = c.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.width)]; }) };
  }
  const surf = document.querySelector('.cr-surface');
  if (surf) { const r = surf.getBoundingClientRect(); out.surface = { x: Math.round(r.x), w: Math.round(r.width), margin: cs(surf).margin }; }
  const lp = document.querySelector('.landing-page');
  if (lp) { out.landingPadding = cs(lp).padding; }
  const grids = [...document.querySelectorAll('.landing-card-grid')].filter(vis);
  out.landingGrids = grids.map((g) => ({ cardHeights: [...g.children].filter(vis).map((c) => Math.round(c.getBoundingClientRect().height)) }));
  const empties = [...document.querySelectorAll('.cr-empty')].filter(vis);
  out.emptyStates = empties.map((e) => e.textContent.trim().slice(0, 60));
  return out;
};

const run = async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const results = {};
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    const page = await ctx.newPage();
    for (const s of SURFACES) {
      if (filter && !s.slug.includes(filter)) continue;
      try {
        await page.goto(BASE + s.url, { waitUntil: 'networkidle', timeout: 20000 });
      } catch { /* proceed with whatever rendered */ }
      if (s.tab) {
        const btn = page.locator('.cr-tabs button', { hasText: s.tab }).first();
        try { await btn.click({ timeout: 5000 }); await page.waitForTimeout(600); } catch { results[`${s.slug}-${vp.name}`] = { error: 'tab not clickable' }; continue; }
      }
      await page.waitForTimeout(400);
      const shot = path.join(OUT, `${s.slug}-${vp.name}-${loopTag}.png`);
      await page.screenshot({ path: shot, fullPage: true });
      results[`${s.slug}-${vp.name}`] = await page.evaluate(MEASURE);
    }
    await ctx.close();
  }
  await browser.close();
  const jsonPath = path.join(OUT, `metrics-${loopTag}${filter ? `-${filter}` : ''}.json`);
  fs.writeFileSync(jsonPath, JSON.stringify(results, null, 1));
  console.log(`written ${jsonPath}`);
};

run().catch((err) => { console.error(err); process.exit(1); });
