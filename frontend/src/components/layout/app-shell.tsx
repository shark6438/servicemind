"use client";

import {
  Activity,
  BookOpenCheck,
  ClipboardCheck,
  FileClock,
  Gauge,
  LogOut,
  Menu,
  Network,
  ShieldCheck,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/providers/auth-provider";

const navigation = [
  { href: "/", label: "工作台", icon: Activity },
  { href: "/runs", label: "运行记录", icon: Network },
  { href: "/approvals", label: "审批中心", icon: ClipboardCheck, role: "approver" },
  { href: "/memory", label: "记忆复核", icon: BookOpenCheck, role: "approver" },
  {
    href: "/audit",
    label: "审计记录",
    icon: FileClock,
    anyRole: ["operator", "approver", "tenant_admin"],
  },
  { href: "/evaluation", label: "质量与发布", icon: Gauge },
] as const;

function LoginScreen() {
  const { ready, login } = useAuth();
  return (
    <main className="login-shell">
      <section className="login-brand" aria-labelledby="login-title">
        <div className="login-product"><ShieldCheck aria-hidden="true" /><strong>ServiceMind 运维控制台</strong></div>
        <h1 id="login-title">企业 IT 服务管理</h1>
        <p className="login-lead">统一查看工单调查、证据、审批决定和审计记录。</p>
        <ul className="login-capabilities">
          <li>租户与数据范围隔离</li>
          <li>高风险操作人工审批</li>
          <li>全过程审计追踪</li>
        </ul>
      </section>
      <section className="login-action" aria-label="登录">
        <h2>登录</h2>
        <p>使用企业身份进入当前租户的运维工作区。</p>
        <button className="button button--primary button--wide" disabled={!ready} onClick={() => void login()}>
          {ready ? "使用企业身份登录" : "正在连接身份服务"}
        </button>
        <p className="login-fineprint">访问权限由身份角色、租户、实体和组范围共同决定。</p>
      </section>
    </main>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  if (!auth.ready || !auth.authenticated) return <LoginScreen />;
  const visible = navigation
    .filter((item) => !("role" in item) || auth.roles.has(item.role))
    .filter((item) => !("anyRole" in item) || item.anyRole.some((role) => auth.roles.has(role)));

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="mobile-header">
        <Link href="/" className="mobile-brand">ServiceMind</Link>
        <button className="icon-button" aria-label={open ? "关闭导航" : "打开导航"} onClick={() => setOpen((value) => !value)}>
          {open ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
        </button>
      </header>
      <aside className={`sidebar ${open ? "sidebar--open" : ""}`}>
        <div className="brand">
          <ShieldCheck aria-hidden="true" />
          <div><strong>ServiceMind</strong><small>运维控制台</small></div>
        </div>
        <nav className="sidebar-nav" aria-label="主导航">
          {visible.map(({ href, label, icon: Icon }) => {
            const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link key={href} href={href} className={active ? "nav-link nav-link--active" : "nav-link"} aria-current={active ? "page" : undefined} onClick={() => setOpen(false)}>
                <Icon aria-hidden="true" />{label}
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <div className="identity"><span className="identity-dot" /><div><strong>{auth.username}</strong><small>{auth.tenantId ? `租户 ${auth.tenantId.slice(0, 8)}` : "租户已验证"}</small></div></div>
          <button className="logout" onClick={() => void auth.logout()}><LogOut aria-hidden="true" />退出登录</button>
        </div>
      </aside>
      {open && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setOpen(false)} />}
      <main className="main" id="main-content">
        <header className="topbar"><span>受控环境</span><Link href="/evaluation">查看质量与发布状态</Link></header>
        {children}
      </main>
    </div>
  );
}
