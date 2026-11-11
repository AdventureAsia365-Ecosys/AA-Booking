// e2e/signed-in/helpers/cognitoSignIn.ts
// AA-1073 / AA-1074 (signed-in smoke, design e2e/SIGNED-IN-SMOKE.md) — the managed-login email-code steps. COPIED from
// AA-CIS-App tests/e2e/helpers/cognitoSignIn.ts (CIS origin/main 8137655, AA-816); repos do not import across each
// other. Changes: the address is a required parameter (no CIS default), no screenshot hooks (the Booking setup takes
// none on the managed-login host), and the email field is filled until it keeps its value (lesson S253: the managed
// login hydrates late and drops a value typed too early).
//
// The code is a live credential: it is never logged or printed, and nothing captures the page after it is typed
// (the setup project runs with trace and screenshots off).
import { expect, type Locator, type Page } from '@playwright/test';
import { MAILBOX_PREFIX, mailboxBaseline, waitForSignInCode } from './mailbox';

export const STAFF_AUTH_HOST = 'staff-auth.lumiguides.it.com';

/** True while the page is on the managed-login host. */
export function onStaffAuth(page: Page): boolean {
  try {
    return new URL(page.url()).host === STAFF_AUTH_HOST;
  } catch {
    return false;
  }
}

/** After "Next": either the code page directly (passwordless user), or a sign-in method choice first. */
export async function reachCodeStep(page: Page): Promise<Locator> {
  const codeField = page.getByLabel(/verification code|sign-in code|^code$/i);
  await expect
    .poll(
      async () => {
        if (await codeField.first().isVisible().catch(() => false)) return 'code';
        // A user with a password gets a method choice; pick the email code option.
        const emailChoice = page
          .getByRole('button', { name: /email|one-time code/i })
          .or(page.getByRole('radio', { name: /email/i }))
          .first();
        if (await emailChoice.isVisible().catch(() => false)) {
          await emailChoice.click({ timeout: 5000 });
          const next = page.getByRole('button', { name: /^(next|continue)$/i }).first();
          if (await next.isVisible().catch(() => false)) await next.click({ timeout: 5000 }).catch(() => {});
        }
        return 'waiting';
      },
      { timeout: 30000, intervals: [500, 1000, 2000] },
    )
    .toBe('code');
  return codeField.first();
}

/**
 * On the managed-login email form: type `email`, ask for a code, read it from the mailbox (`prefix`, default
 * inbound/), submit it. Every wait is capped; the mailbox wait has its own 90 s cap.
 */
export async function signInByEmailCode(page: Page, opts: { email: string; prefix?: string }): Promise<void> {
  const prefix = opts.prefix ?? MAILBOX_PREFIX;
  const emailField = page.getByLabel(/email/i).first();
  await expect(emailField).toBeVisible({ timeout: 15000 });
  // Fill until kept: a value typed before the page script attaches is lost (lesson S253, AA-979).
  await expect(async () => {
    await emailField.fill(opts.email, { timeout: 5000 });
    await expect(emailField).toHaveValue(opts.email, { timeout: 1000 });
  }).toPass({ timeout: 15000, intervals: [500, 1000, 2000] });

  // Snapshot the mailbox BEFORE asking for a code, then ask.
  const baseline = await mailboxBaseline(prefix);
  const since = new Date();
  await page.getByRole('button', { name: /^next$/i }).click({ timeout: 10000 });
  const codeField = await reachCodeStep(page);

  const code = await waitForSignInCode({ since, baseline, timeoutMs: 90000, recipient: opts.email, prefix });
  await codeField.fill(code);
  await page.getByRole('button', { name: /^(continue|sign in|next)$/i }).first().click({ timeout: 10000 });
}
