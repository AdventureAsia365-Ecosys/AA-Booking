// ui/booking/staffRoles.ts — AA-1155 T7: pure helpers of the role editor page (app/(staff)/roles), tested by
// ui/booking/staffRoles.test.mjs. The API is the authority (400 on an approve-class or unknown capability); these only
// shape what the page offers.

/** The bk-062 / CIS 462 code pattern of a custom role (never a built-in role name). */
export const CUSTOM_CODE = /^custom_[a-z][a-z0-9_]{1,46}$/;
/** The longest custom code (bk-062 CHECK): `custom_` + 47. */
const CODE_LENGTH = 54;

/** A code suggested from a label: `custom_` + the label in lower case, letters and digits joined by `_`. Empty when the
 *  label has no letter to start with. */
export function suggestCode(label: string): string {
  const slug = label
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^[^a-z]+/, "")
    .replace(/_+$/, "");
  const code = `custom_${slug}`.slice(0, CODE_LENGTH).replace(/_+$/, "");
  return CUSTOM_CODE.test(code) ? code : "";
}

export interface CapabilityChoice {
  name: string;
  assignable: boolean;
}

export interface CapabilityGroup {
  /** The part before the first dot (`supply`, `pricing`, ...). */
  area: string;
  items: CapabilityChoice[];
}

/** Capabilities grouped by area, areas and names in alphabetical order. */
export function groupCapabilities(capabilities: readonly CapabilityChoice[]): CapabilityGroup[] {
  const byArea = new Map<string, CapabilityChoice[]>();
  for (const c of capabilities) {
    const area = c.name.split(".")[0] ?? c.name;
    byArea.set(area, [...(byArea.get(area) ?? []), c]);
  }
  return [...byArea.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([area, items]) => ({ area, items: [...items].sort((x, y) => x.name.localeCompare(y.name)) }));
}

/** The capabilities a clone of a built-in role starts with: the role's, without the approve class (what the API does
 *  when no list is sent). */
export function cloneOf(builtIn: readonly string[], capabilities: readonly CapabilityChoice[]): string[] {
  const assignable = new Set(capabilities.filter((c) => c.assignable).map((c) => c.name));
  return builtIn.filter((c) => assignable.has(c)).sort();
}

/** Tick or untick one capability (never an approve-class one). */
export function toggle(selected: readonly string[], name: string, on: boolean, capabilities: readonly CapabilityChoice[]): string[] {
  const allowed = capabilities.some((c) => c.name === name && c.assignable);
  const next = new Set(selected);
  if (on && allowed) next.add(name);
  else next.delete(name);
  return [...next].sort();
}

/** Two capability lists hold the same names. */
export function sameSet(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && [...a].sort().every((x, i) => x === [...b].sort()[i]);
}
