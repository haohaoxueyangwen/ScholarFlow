import type { Metadata } from "next";
import "./globals.css";
import "./desk-workbench.css";

export const metadata: Metadata = {
  title: "ScholarFlow Research Workbench",
  description: "A local-first, evidence-grounded graduate research workspace.",
  icons: { icon: "/brand/scholarflow-icon.png" },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
