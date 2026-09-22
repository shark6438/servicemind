"use client";
import { Activity, BookOpenCheck, ClipboardCheck, FileClock, Gauge, LogOut, Menu, Network, ShieldCheck, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/providers/auth-provider";

const navigation = [
  { href: "/", label: "工作台", icon: Activity }, { href: "/runs", label: "运行账本", icon: Network },
  { href: "/approvals", label: "审批中心", icon: ClipboardCheck, role: "approver" },
  { href: "/memory", label: "记忆复核", icon: BookOpenCheck, role: "approver" },
  { href: "/audit", label: "审计追踪", icon: FileClock, anyRole: ["operator", "approver", "tenant_admin"] },
  { href: "/evaluation", label: "质量门禁", icon: Gauge },
] as const;

function LoginScreen() { const { ready, login } = useAuth(); return <main className="login-shell"><section className="login-brand" aria-labelledby="login-title"><div className="brand-mark brand-mark--large"><ShieldCheck aria-hidden="true" /></div><p className="eyebrow">ServiceMind / 企业 IT 智能体控制平面</p><h1 id="login-title">每个判断都有证据，<br />每次执行都能追溯。</h1><p className="login-lead">在一个受租户隔离和审批门禁保护的界面中，检查工单、Agent 轨迹、证据、操作意图与审计记录。</p><div className="login-rule"><span>OIDC + PKCE</span><span>租户隔离</span><span>追加式审计</span></div></section><section className="login-action" aria-label="登录"><p className="section-kicker">受控访问</p><h2>进入运维控制台</h2><p>身份、租户、实体范围与角色由 Keycloak 签发，浏览器不持久化访问令牌。</p><button className="button button--primary button--wide" disabled={!ready} onClick={() => void login()}>{ready ? "使用企业身份登录" : "正在连接身份服务"}</button><p className="login-fineprint">登录后仍由 API 对每个操作执行角色与数据范围校验。</p></section></main>; }

export function AppShell({ children }: { children: React.ReactNode }) {
  const auth = useAuth(); const pathname = usePathname(); const [open, setOpen] = useState(false);
  if (!auth.ready || !auth.authenticated) return <LoginScreen />;
  const visible = navigation.filter((item) => !("role" in item) || auth.roles.has(item.role)).filter((item) => !("anyRole" in item) || item.anyRole.some((role) => auth.roles.has(role)));
  return <div className="app-shell"><a className="skip-link" href="#main-content">跳到主要内容</a><header className="mobile-header"><Link href="/" className="mobile-brand">SM / OPS</Link><button className="icon-button" aria-label={open ? "关闭导航" : "打开导航"} onClick={() => setOpen((v) => !v)}>{open ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}</button></header><aside className={`sidebar ${open ? "sidebar--open" : ""}`}><div className="brand"><span className="brand-mark"><ShieldCheck aria-hidden="true" /></span><div><strong>ServiceMind</strong><small>运营控制平面</small></div></div><nav className="sidebar-nav" aria-label="主导航">{visible.map(({ href, label, icon: Icon }) => { const active = href === "/" ? pathname === "/" : pathname.startsWith(href); return <Link key={href} href={href} className={active ? "nav-link nav-link--active" : "nav-link"} aria-current={active ? "page" : undefined} onClick={() => setOpen(false)}><Icon aria-hidden="true" />{label}</Link>; })}</nav><div className="sidebar-foot"><div className="identity"><span className="identity-dot" /><div><strong>{auth.username}</strong><small>{auth.tenantId ? `租户 ${auth.tenantId.slice(0, 8)}` : "租户上下文已验证"}</small></div></div><button className="logout" onClick={() => void auth.logout()}><LogOut aria-hidden="true" />退出</button></div></aside>{open && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setOpen(false)} />}<main className="main" id="main-content"><header className="topbar"><div className="environment"><span>LIVE</span> 受控环境</div><Link className="topbar-link" href="/evaluation">查看发布门禁</Link></header>{children}</main></div>;
}
