/**
 * E2E coverage for the public 60-second demo widget on the landing page.
 *
 * Backend calls (capabilities, demo session/call), the Cloudflare Turnstile
 * script, and the conversation WebSocket are all mocked, so these tests prove
 * UI wiring without a live backend. Gate/endpoint behaviour is covered by
 * pytest. Availability is runtime configuration: the widget renders from
 * GET /api/v1/demo/capabilities (or the window.TURNSTILE_SITE_KEY test
 * override, which short-circuits the fetch).
 */

import { test, expect } from '@playwright/test';

/** Serve a Turnstile stub that issues a token shortly after render. */
async function stubTurnstileScript(page) {
  await page.route('https://challenges.cloudflare.com/**', async (route) => {
    await route.fulfill({
      contentType: 'text/javascript',
      body: `
        window.turnstile = {
          render(el, opts) {
            el.setAttribute('data-turnstile', 'rendered');
            window.__turnstileIssue = () => opts.callback('tok-e2e');
            setTimeout(window.__turnstileIssue, 50);
            return 1;
          },
          reset() {},
        };
      `,
    });
  });
}

/** Stub Turnstile before any page script runs, and expose the site key. */
async function installDemoEnvironment(page) {
  await page.addInitScript(() => {
    window.TURNSTILE_SITE_KEY = 'test-site-key';
  });
  await stubTurnstileScript(page);
}

/** Drive the widget through the runtime capabilities contract instead. */
async function installCapabilitiesEnvironment(page, capabilities) {
  await page.route('**/api/v1/demo/capabilities', async (route) => {
    await route.fulfill({ json: capabilities });
  });
  await stubTurnstileScript(page);
}

test.describe('landing page demo widget', () => {
  test('renders both demo modes and enables submit only after captcha', async ({ page }) => {
    await installDemoEnvironment(page);
    await page.goto('/');

    const widget = page.locator('.demo-widget');
    await expect(widget).toBeVisible();
    await expect(widget.getByRole('tab', { name: /In your browser/ })).toBeVisible();
    await expect(widget.getByRole('tab', { name: /Call my phone/ })).toBeVisible();

    // Captcha container rendered by the stub; submit enables once the token arrives.
    await expect(widget.locator('.demo-widget-captcha')).toHaveAttribute('data-turnstile', 'rendered');
    await expect(widget.getByRole('button', { name: 'Start voice demo' })).toBeEnabled();
  });

  test('browser demo: starts a capped session and shows live transcript', async ({ page }) => {
    await installDemoEnvironment(page);

    const sessionRequests = [];
    await page.route('**/api/v1/demo/session', async (route) => {
      sessionRequests.push(JSON.parse(route.request().postData() || '{}'));
      await route.fulfill({
        json: { demo_token: 'signed-demo-token', expires_in_seconds: 300, max_demo_seconds: 60 },
      });
    });

    // Fake mic: the widget only needs a MediaStream-shaped object.
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext();
        const dest = ctx.createMediaStreamDestination();
        return dest.stream;
      };
    });

    // Mock the conversation WebSocket and greet with an assistant message.
    let wsUrl = '';
    await page.routeWebSocket(/\/api\/v1\/browser\/conversation/, (ws) => {
      wsUrl = ws.url();
      ws.onMessage(() => {}); // swallow mic audio frames
      ws.send(
        JSON.stringify({
          type: 'assistant',
          content: 'Hi! This is the Wulo Clinic Recall demo for Riverside Physiotherapy.',
        }),
      );
    });

    await page.goto('/');
    const widget = page.locator('.demo-widget');
    await widget.getByLabel('Work email').fill('owner@clinic.example.com');
    await widget.getByLabel('Clinic name').fill('Riverside Physio');
    const start = widget.getByRole('button', { name: 'Start voice demo' });
    await expect(start).toBeEnabled();
    await start.click();

    // Session request carried the gate fields.
    await expect.poll(() => sessionRequests.length).toBe(1);
    expect(sessionRequests[0]).toMatchObject({
      work_email: 'owner@clinic.example.com',
      clinic_name: 'Riverside Physio',
      turnstile_token: 'tok-e2e',
    });

    // Live session: countdown + transcript from the mocked socket.
    await expect(widget.locator('.demo-widget-countdown')).toBeVisible();
    await expect(widget.locator('.demo-widget-countdown strong')).toHaveText(/^\d+s$/);
    await expect(widget.locator('.demo-widget-transcript')).toContainText(
      'Wulo Clinic Recall demo',
    );
    expect(wsUrl).toContain('demo_token=signed-demo-token');

    // Manual end → wrap-up status.
    await widget.getByRole('button', { name: 'End demo' }).click();
    await expect(widget.locator('.demo-widget-status')).toContainText("That's the demo");
  });

  test('phone demo: posts UK number and shows calling status', async ({ page }) => {
    await installDemoEnvironment(page);

    const callRequests = [];
    await page.route('**/api/v1/demo/call', async (route) => {
      callRequests.push(JSON.parse(route.request().postData() || '{}'));
      await route.fulfill({ json: { status: 'calling', max_demo_seconds: 60 } });
    });

    await page.goto('/');
    const widget = page.locator('.demo-widget');
    await widget.getByRole('tab', { name: /Call my phone/ }).click();
    await widget.getByLabel('Work email').fill('owner@clinic.example.com');
    await widget.getByLabel('Clinic name').fill('Riverside Physio');
    await widget.getByLabel('UK phone number').fill('07700 900123');

    const call = widget.getByRole('button', { name: 'Call me now' });
    await expect(call).toBeEnabled();
    await call.click();

    await expect.poll(() => callRequests.length).toBe(1);
    expect(callRequests[0]).toMatchObject({
      work_email: 'owner@clinic.example.com',
      clinic_name: 'Riverside Physio',
      phone_number: '07700 900123',
      turnstile_token: 'tok-e2e',
    });
    await expect(widget.locator('.demo-widget-status')).toContainText('Calling you now');
  });

  test('shows a friendly error when the demo gate rejects the request', async ({ page }) => {
    await installDemoEnvironment(page);

    await page.route('**/api/v1/demo/session', async (route) => {
      await route.fulfill({ status: 429, json: { detail: 'too_many_requests' } });
    });

    await page.goto('/');
    const widget = page.locator('.demo-widget');
    await widget.getByLabel('Work email').fill('owner@clinic.example.com');
    await widget.getByLabel('Clinic name').fill('Riverside Physio');
    const start = widget.getByRole('button', { name: 'Start voice demo' });
    await expect(start).toBeEnabled();
    await start.click();

    await expect(widget.locator('.demo-widget-error')).toContainText(
      'Too many demo requests',
    );
  });

  test('widget is hidden when the backend reports the demo off', async ({ page }) => {
    await installCapabilitiesEnvironment(page, {
      experience: 'off',
      browser_enabled: false,
      phone_enabled: false,
      max_demo_seconds: 60,
      turnstile_site_key: '',
    });
    await page.goto('/');
    await expect(page.locator('.landing-hero')).toBeVisible();
    await expect(page.locator('.demo-widget')).toHaveCount(0);
  });

  test('widget is hidden when the capabilities endpoint errors', async ({ page }) => {
    await page.route('**/api/v1/demo/capabilities', async (route) => {
      await route.fulfill({ status: 500, json: { detail: 'boom' } });
    });
    await page.goto('/');
    await expect(page.locator('.landing-hero')).toBeVisible();
    await expect(page.locator('.demo-widget')).toHaveCount(0);
  });

  test('runtime capabilities render the widget and hide a disabled phone mode', async ({ page }) => {
    await installCapabilitiesEnvironment(page, {
      experience: 'legacy',
      browser_enabled: true,
      phone_enabled: false,
      max_demo_seconds: 45,
      turnstile_site_key: 'runtime-site-key',
    });
    await page.goto('/');

    const widget = page.locator('.demo-widget');
    await expect(widget).toBeVisible();
    await expect(widget.getByRole('tab', { name: /In your browser/ })).toBeVisible();
    await expect(widget.getByRole('tab', { name: /Call my phone/ })).toHaveCount(0);
    await expect(widget.locator('.demo-widget-captcha')).toHaveAttribute('data-turnstile', 'rendered');
    await expect(widget.locator('.demo-widget-smallprint')).toContainText('45 seconds');
  });
});
