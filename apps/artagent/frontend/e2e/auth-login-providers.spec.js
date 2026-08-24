/**
 * E2E coverage for login-provider rendering on the landing page.
 *
 * Google sign-in is opt-in per environment (ENABLE_GOOGLE_LOGIN at container
 * start, VITE_ENABLE_GOOGLE_LOGIN in dev, window.ENABLE_GOOGLE_LOGIN test
 * override). Microsoft remains the default provider in every configuration.
 */

import { test, expect } from '@playwright/test';

const MICROSOFT_LOGIN = '/.auth/login/aad?post_login_redirect_uri=/app';
const GOOGLE_LOGIN = '/.auth/login/google?post_login_redirect_uri=/app';

test.describe('login providers', () => {
  test('Google button is hidden when the flag is off', async ({ page }) => {
    await page.addInitScript(() => {
      window.ENABLE_GOOGLE_LOGIN = false;
    });
    await page.goto('/');

    const microsoft = page.getByRole('link', { name: 'Sign in with Microsoft' });
    await expect(microsoft).toBeVisible();
    await expect(microsoft).toHaveAttribute('href', MICROSOFT_LOGIN);
    await expect(page.getByRole('link', { name: 'Sign in with Google' })).toHaveCount(0);
  });

  test('Google button renders next to Microsoft when the flag is on', async ({ page }) => {
    await page.addInitScript(() => {
      window.ENABLE_GOOGLE_LOGIN = true;
    });
    await page.goto('/');

    const microsoft = page.getByRole('link', { name: 'Sign in with Microsoft' });
    const google = page.getByRole('link', { name: 'Sign in with Google' });
    await expect(microsoft).toBeVisible();
    await expect(microsoft).toHaveAttribute('href', MICROSOFT_LOGIN);
    await expect(google).toBeVisible();
    await expect(google).toHaveAttribute('href', GOOGLE_LOGIN);
  });
});

test.describe('provider-choice sign-in screen at /app', () => {
  test('unauthenticated visit shows both providers instead of redirecting to Microsoft', async ({ page }) => {
    await page.addInitScript(() => {
      window.ENABLE_AUTH_CHECK = true;
      window.ENABLE_GOOGLE_LOGIN = true;
    });
    await page.route('**/.auth/me', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
    );
    await page.goto('/app');

    await expect(page.getByRole('heading', { name: 'Sign in to Wulo-X' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Continue with Microsoft' })).toHaveAttribute('href', MICROSOFT_LOGIN);
    await expect(page.getByRole('link', { name: 'Continue with Google' })).toHaveAttribute('href', GOOGLE_LOGIN);
    expect(page.url()).toContain('/app');
  });

  test('Google option is hidden on the sign-in screen when the flag is off', async ({ page }) => {
    await page.addInitScript(() => {
      window.ENABLE_AUTH_CHECK = true;
      window.ENABLE_GOOGLE_LOGIN = false;
    });
    await page.route('**/.auth/me', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
    );
    await page.goto('/app');

    await expect(page.getByRole('link', { name: 'Continue with Microsoft' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Continue with Google' })).toHaveCount(0);
  });

  test('authenticated shell shows a sign-out link that returns to the homepage', async ({ page }) => {
    await page.addInitScript(() => {
      window.ENABLE_AUTH_CHECK = true;
    });
    await page.route('**/.auth/me', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ userDetails: 'staff@example.test', claims: [{ typ: 'roles', val: 'staff' }] }]),
      }),
    );
    await page.route('**/api/v1/**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );
    await page.goto('/app');

    const signOut = page.getByRole('link', { name: 'Sign out' });
    await expect(signOut).toBeVisible();
    await expect(signOut).toHaveAttribute('href', '/.auth/logout?post_logout_redirect_uri=/');
  });
});
