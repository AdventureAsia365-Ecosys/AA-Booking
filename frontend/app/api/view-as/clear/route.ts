// app/api/view-as/clear/route.ts — AA-1157: the staff layout lands here when the API ended the owner's view (a server
// component cannot set cookies): the view-as cookie is cleared and the browser goes back to the dashboard.
import { NextRequest, NextResponse } from "next/server";
import { clearViewAsCookie } from "@/lib/viewAsCookie";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  return clearViewAsCookie(NextResponse.redirect(new URL("/", req.url)));
}
