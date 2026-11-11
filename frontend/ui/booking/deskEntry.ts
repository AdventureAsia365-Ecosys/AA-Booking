// ui/booking/deskEntry.ts
// AA-1075 (bk-11) — pure helpers of the B2B desk "New agency booking" form: the request lines sent to
// POST /booking/b2b-desk/entries (the tour line with its room mix, then one line per extra hotel room or service, each
// for the same travellers) and the default room mix the engine uses when none is given (app/b2b/components.py
// room_mix: party // 2 doubles plus one single when the party is odd).
//
// Import-free on purpose: deskEntry.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

export type DeskOccupancy = "single" | "double" | "twin" | "triple" | "family";
export interface DeskRoom {
  occupancy: DeskOccupancy;
  count: number;
}
export interface DeskPax {
  adult: number;
  child: number;
  infant: number;
}
export interface DeskExtra {
  kind: "room_type" | "service";
  id: string;
  /** Empty = the tour's start date. */
  date: string;
  /** Room nights (rooms only); empty or below 1 = 1. */
  nights: string;
}
export interface DeskLine {
  line_ref: string;
  component_id: string;
  departure_id?: string | null;
  service_date: string;
  nights?: number | null;
  pax: DeskPax;
  rooms: DeskRoom[];
}

/** The rooms that hold at least one room (a row left at 0 is dropped). */
export function roomMix(rooms: DeskRoom[]): DeskRoom[] {
  return rooms.filter((r) => Number.isInteger(r.count) && r.count >= 1).map((r) => ({ occupancy: r.occupancy, count: r.count }));
}

/** "2 doubles + 1 single" for a party, as the engine sets the rooms when the form sends none. */
export function defaultRoomsLabel(party: number): string {
  if (!party || party < 1) return "no rooms";
  const doubles = Math.floor(party / 2);
  const parts = [];
  if (doubles) parts.push(`${doubles} double${doubles === 1 ? "" : "s"}`);
  if (party % 2) parts.push("1 single");
  return parts.join(" + ");
}

/** The request lines: the tour (``tp:``) with its departure and room mix, then each extra (``rt:`` with nights and the
 *  same room mix, ``sv:`` without rooms), dated on the tour's start unless the extra has its own date. */
export function deskLines(input: {
  planId: string;
  departureId: string | null;
  start: string;
  pax: DeskPax;
  rooms: DeskRoom[];
  extras: DeskExtra[];
}): DeskLine[] {
  const mix = roomMix(input.rooms);
  const tour: DeskLine = {
    line_ref: "tour",
    component_id: `tp:${input.planId}`,
    departure_id: input.departureId,
    service_date: input.start,
    pax: input.pax,
    rooms: mix,
  };
  return [
    tour,
    ...input.extras.map((x, i): DeskLine => {
      const room = x.kind === "room_type";
      return {
        line_ref: `extra-${i + 1}`,
        component_id: `${room ? "rt" : "sv"}:${x.id}`,
        service_date: x.date || input.start,
        nights: room ? Math.max(1, Math.floor(Number(x.nights)) || 1) : null,
        pax: input.pax,
        rooms: room ? mix : [],
      };
    }),
  ];
}
