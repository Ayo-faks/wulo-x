/**
 * Control-room screenshot generator for docs/assets/control-room-inbox.png
 * (the hero image in README.md).
 *
 * Renders the Clinic Recall control room with deterministic, synthetic route
 * mocks (no backend, no real patient data) and captures the Inbox escalation
 * queue. Spec-injected CSS hides the workspace switch and operator rail so the
 * image focuses on the control room itself; no application code is modified.
 *
 * Regenerate with:
 *   cd apps/artagent/frontend
 *   npx playwright test e2e/control-room-screenshot.spec.js
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';
import { installWalkthroughMocks } from './helpers/clinic-recall-walkthrough-mocks.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(__dirname, '../../../../docs/assets');

test.use({
  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 2,
});

test('control room inbox hero screenshot', async ({ page }) => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  await page.addInitScript(() => window.localStorage.clear());
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await installWalkthroughMocks(page);

  await page.goto('/app');
  await expect(page.getByRole('heading', { name: 'Escalation and pending booking queue' })).toBeVisible();
  await expect(page.getByText('Amina Example')).toBeVisible();
  await expect(page.getByText('Bayo Example')).toBeVisible();
  await expect(page.getByText('Chidi Example')).toBeVisible();

  // Screenshot-only framing: drop the workspace switch + operator rail, recenter.
  await page.addStyleTag({
    content: `
      .product-brand, .product-switch, .shell-rail { display: none !important; }
      .app-shell { padding: 44px 64px 44px 64px; }
      .shell-topbar, .shell-main { width: min(1240px, calc(100vw - 128px)); }
    `,
  });
  await page.waitForTimeout(300);

  await page.screenshot({ path: path.join(OUT_DIR, 'control-room-inbox.png'), fullPage: false });
});
