// app/layout.tsx
// AA-683 — root layout: brand fonts (Fahkwang titles, Poppins UI, JetBrains Mono codes, Satisfy for the
// login splash line only; design system §2.2), the theme + sidebar restore scripts (before paint, no
// flash; keys bk_theme / bk_sidebar) and the app-wide providers (react-query with the Booking 401 / retry
// rules in ui/booking/QueryProvider.tsx, toasts).

import type { Metadata, Viewport } from "next";
import "./globals.css";
import { BookingQueryProvider } from "@/ui/booking/QueryProvider";
import { ToastProvider } from "@/ui/Toast";
import { BRAND } from "@/ui/brand";

export const metadata: Metadata = {
  title: { default: "Booking · Adventure Asia", template: "%s · Booking" },
  description: "Adventure Asia staff booking admin: trip cases, proposals, customers.",
  robots: { index: false, follow: false },
  icons: { icon: "/assets/adventure-asia-logo.png" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: BRAND.ink, // navy, as the site header (design system §2.1: not the leftover orange)
};

const THEME_RESTORE = `(function(){try{var c=localStorage.getItem('bk_theme');var r=(c==='light'||c==='dark')?c:(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');document.documentElement.setAttribute('data-theme',r);}catch(e){document.documentElement.setAttribute('data-theme','light');}})();`;
const SIDEBAR_RESTORE = `(function(){try{var s=localStorage.getItem('bk_sidebar');document.documentElement.setAttribute('data-sidebar',s==='collapsed'?'collapsed':'expanded');}catch(e){document.documentElement.setAttribute('data-sidebar','expanded');}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_RESTORE }} />
        <script dangerouslySetInnerHTML={{ __html: SIDEBAR_RESTORE }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        {/* eslint-disable-next-line @next/next/no-page-custom-font -- same Google Fonts link as the CIS kit (AA-605) */}
        <link
          href="https://fonts.googleapis.com/css2?family=Fahkwang:wght@500;600;700&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Satisfy&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <BookingQueryProvider>
          <ToastProvider>{children}</ToastProvider>
        </BookingQueryProvider>
      </body>
    </html>
  );
}
