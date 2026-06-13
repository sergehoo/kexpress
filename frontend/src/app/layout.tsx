import type { Metadata, Viewport } from "next";
import "./globals.css";
import { Providers } from "./providers";
import { SWRegister } from "@/components/SWRegister";
import { themeInitScript } from "@/lib/theme";

export const metadata: Metadata = {
  title: "Kaydan Express — Gestion de flotte",
  description: "Plateforme de gestion de flotte de véhicules multi-filiales.",
  manifest: "/manifest.webmanifest",
  // Favicon explicite (logo emblème) en plus des icônes App Router auto-détectées.
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "16x16 32x32 48x48" },
      { url: "/icons/icon-192.png", type: "image/png", sizes: "192x192" },
    ],
    apple: "/icons/apple-touch-icon.png",
    shortcut: "/favicon.ico",
  },
  appleWebApp: { capable: true, statusBarStyle: "default", title: "Kaydan Express" },
};

export const viewport: Viewport = {
  themeColor: "#0b1322",
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <Providers>{children}</Providers>
        <SWRegister />
      </body>
    </html>
  );
}
