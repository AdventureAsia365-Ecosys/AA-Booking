// e2e/signed-in/departure-board.spec.ts
// AA-1073 rest signed-in smoke (bk-04, bk-16, bk-09, bk-17): Operations → Departures shows the month calendar and the
// table of every fixed departure (GET b2b-desk/departure-board answers 200 with totals and filter options), and a
// departure opens the drawer with its sections: Seats by agency, Ops, Departure fees, Price. Read-only: nothing is
// saved, proposed or approved (the E2E user gets 403 e2e_read_only on writes anyway).
import { expect, test } from "@playwright/test";
import { assertClean, bffGet, capture, firstTableItem, pageSettled, tableSettled, watch } from "./helpers/page";

type BoardPage = {
  items: { id: string; start_date: string }[];
  total: number;
  totals: { departures: number };
  countries: string[];
  plans: { source_id: string }[];
};

test("departure board: calendar, table and the departure drawer", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/departures", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  await expect(page.getByTestId("board-month"), "the month header").toBeVisible({ timeout: 15_000 });
  const board = await bffGet<BoardPage>(page, "b2b-desk/departure-board");
  expect(board.status, "GET b2b-desk/departure-board").toBe(200);
  expect(typeof board.body!.totals.departures, "the board carries its totals").toBe("number");
  expect(Array.isArray(board.body!.plans), "the board carries its trip plan options").toBe(true);
  test.info().annotations.push({ type: "board", description: `departures this month: ${board.body!.total}` });
  await capture(page, "departure-board", async () => {
    await pageSettled(page);
    await expect(page.getByTestId("board-month")).toBeVisible({ timeout: 15_000 });
    // S277: the month header renders before the board query answers; wait for the calendar to be filled (a chip or
    // the empty month), never capture the loading state.
    await expect(page.locator('[data-testid="board-calendar"][aria-busy="true"], [data-testid="board-day-list"][aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
    if (board.body!.total > 0) {
      await expect(page.locator('[data-testid="board-chip"]:visible').first()).toBeVisible({ timeout: 30_000 });
    }
  });

  await page.goto("/departures?view=table", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  const state = await tableSettled(page.locator("main").first());
  await capture(page, "departure-board-table", async () => {
    await pageSettled(page);
    await tableSettled(page.locator("main").first());
  }, () => firstTableItem(page));

  const first = board.body!.items[0];
  if (state === "empty" || !first) {
    test.info().annotations.push({ type: "skip-step", description: "no departure this month on Dev: the drawer was not opened" });
  } else {
    await page.goto(`/departures?view=table&departure=${encodeURIComponent(first.id)}`, { waitUntil: "domcontentloaded", timeout: 30_000 });
    const openDrawer = async () => {
      await pageSettled(page);
      await expect(page.getByTestId("departure-board-drawer")).toBeVisible({ timeout: 30_000 });
    };
    await openDrawer();
    const d = page.getByRole("dialog");
    for (const region of ["Seats by agency", "Ops", "Departure fees", "Price"]) {
      await expect(d.getByRole("region", { name: region }), `drawer section ${region}`).toBeVisible({ timeout: 10_000 });
    }
    await capture(page, "departure-board-drawer", openDrawer, () => page.getByTestId("departure-board-drawer"));
  }
  assertClean(w, "departure-board");
});
