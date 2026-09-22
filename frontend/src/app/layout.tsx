import type { Metadata, Viewport } from "next";
import { connection } from "next/server";
import "@fontsource-variable/noto-sans-sc";
import "@fontsource/ibm-plex-mono/400.css";
import "./globals.css";
import { AppShell } from "@/components/layout/app-shell";
import { AuthProvider } from "@/providers/auth-provider";

export const metadata: Metadata = {
  title: { default: "ServiceMind 运维控制台", template: "%s · ServiceMind" },
  description: "企业 IT 服务管理智能体的运行、证据、审批与审计控制平面。",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { themeColor: "#0b3049", colorScheme: "light" };

export default async function RootLayout({ children }: LayoutProps<"/">) {
  await connection();
  return (
    <html lang="zh-CN">
      <body><AuthProvider><AppShell>{children}</AppShell></AuthProvider></body>
    </html>
  );
}
