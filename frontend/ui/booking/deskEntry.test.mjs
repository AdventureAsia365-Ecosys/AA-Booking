// ui/booking/deskEntry.test.mjs — AA-1075: the B2B desk "New agency booking" request lines and the default rooms.
// Run: node --test ui/booking/deskEntry.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { defaultRoomsLabel, deskLines, roomMix } from "./deskEntry.ts";

const pax = { adult: 3, child: 1, infant: 0 };

test("the tour line carries the room mix; a row left at 0 rooms is dropped", () => {
  const rooms = [{ occupancy: "single", count: 2 }, { occupancy: "twin", count: 1 }, { occupancy: "triple", count: 0 }];
  const [tour] = deskLines({ planId: "p1", departureId: "d1", start: "2027-01-10", pax, rooms, extras: [] });
  assert.deepEqual(tour, {
    line_ref: "tour",
    component_id: "tp:p1",
    departure_id: "d1",
    service_date: "2027-01-10",
    pax,
    rooms: [{ occupancy: "single", count: 2 }, { occupancy: "twin", count: 1 }],
  });
});

test("no rooms = the engine default (empty list)", () => {
  const [tour] = deskLines({ planId: "p1", departureId: null, start: "2027-01-10", pax, rooms: [], extras: [] });
  assert.deepEqual(tour.rooms, []);
  assert.equal(tour.departure_id, null);
});

test("extras: a room night line with nights and the mix, a service without rooms, each on its own or the tour date", () => {
  const extras = [
    { kind: "room_type", id: "r9", date: "", nights: "2" },
    { kind: "service", id: "s4", date: "2027-01-12", nights: "" },
    { kind: "room_type", id: "r8", date: "2027-01-11", nights: "0" },
  ];
  const rooms = [{ occupancy: "double", count: 2 }];
  const [, a, b, c] = deskLines({ planId: "p1", departureId: null, start: "2027-01-10", pax, rooms, extras });
  assert.deepEqual(a, { line_ref: "extra-1", component_id: "rt:r9", service_date: "2027-01-10", nights: 2, pax, rooms });
  assert.deepEqual(b, { line_ref: "extra-2", component_id: "sv:s4", service_date: "2027-01-12", nights: null, pax, rooms: [] });
  assert.equal(c.nights, 1); // below 1 night = 1
});

test("roomMix keeps whole room counts only", () => {
  assert.deepEqual(roomMix([{ occupancy: "family", count: 1.5 }, { occupancy: "double", count: 1 }]), [
    { occupancy: "double", count: 1 },
  ]);
});

test("defaultRoomsLabel names the engine's rooms for a party", () => {
  assert.equal(defaultRoomsLabel(4), "2 doubles");
  assert.equal(defaultRoomsLabel(3), "1 double + 1 single");
  assert.equal(defaultRoomsLabel(1), "1 single");
  assert.equal(defaultRoomsLabel(0), "no rooms");
});
