// lib/approveSplit.ts — AA-1155 T4.1 (make / approve split): which End button a maker-checker row offers, the way the
// API decides it (`may_end` in backend app/b2b/price_adjustments.py and app/b2b/allotments.py). A holder of the
// approval (pricing.approve, b2b_desk.approve) rejects another person's pending row and withdraws any live one; the
// maker withdraws its own pending row with the write right alone (pricing.write, b2b_desk.write).

/** The End button of a live row: "reject" another person's pending row, "withdraw" one's own or one in force. */
export type EndAction = "reject" | "withdraw" | null;

export interface EndRights {
  /** Holds the approval capability. */
  canDecide: boolean;
  /** Holds the write capability that made the row. */
  canWrite: boolean;
  /** The row's maker is the signed-in staff member. */
  isMaker: boolean;
}

export function endAction(state: string, { canDecide, canWrite, isMaker }: EndRights): EndAction {
  if (state === "pending") {
    if (canDecide && !isMaker) return "reject";
    return isMaker && (canDecide || canWrite) ? "withdraw" : null;
  }
  return canDecide && state === "in_force" ? "withdraw" : null;
}
