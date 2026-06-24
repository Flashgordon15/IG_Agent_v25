import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "IG Agent — Quantum Terminal",
  description: "Decoupled Bloomberg Neo trading terminal",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full antialiased">{children}</body>
    </html>
  );
}
