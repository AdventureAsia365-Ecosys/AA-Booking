// e2e/layout/stub-api.mjs
// AA-910 — the only server-side Booking call of the staff pages is GET /booking/staff/me, made by the (staff) layout
// while it renders (app/(staff)/layout.tsx → lib/api/server.ts). Playwright cannot intercept a server-side fetch, so
// the layout check points BOOKING_API_URL of its own `next start` at this stub. Every browser call (the BFF,
// /api/booking/*) is answered by route interception in layout.spec.ts and never reaches a server.
//
// It answers staff/me with e2e/layout/staff-me.json for ANY bearer token (it is a stub; the real Booking API
// verifies the Cognito signature) and 404 for everything else, so a new server-side call shows up as an error page.
// Listens on 127.0.0.1 only. Run: node e2e/layout/stub-api.mjs [port]

import { readFileSync } from "node:fs";
import http from "node:http";

const port = Number(process.argv[2] || process.env.LAYOUT_STUB_PORT || 55481);
const me = readFileSync(new URL("./staff-me.json", import.meta.url), "utf8");

http
  .createServer((req, res) => {
    const path = (req.url || "").split("?")[0];
    if (req.method === "GET" && path === "/booking/staff/me") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(me);
      return;
    }
    if (path === "/health") {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("ok");
      return;
    }
    process.stderr.write(`layout stub: no answer for ${req.method} ${req.url}\n`);
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: "layout_stub_unmocked", message: `No stub answer for ${path}` }));
  })
  .listen(port, "127.0.0.1", () => process.stdout.write(`layout stub on http://127.0.0.1:${port}\n`));
