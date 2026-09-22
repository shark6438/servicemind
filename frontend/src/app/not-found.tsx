import Link from "next/link";
export default function NotFound() { return <section className="page"><div className="fatal"><p className="eyebrow">404 / 未找到</p><h1>此控制台视图不存在</h1><p>地址可能已变更，或该运行记录不在当前租户范围内。</p><Link className="button button--primary" href="/">返回工作台</Link></div></section>; }
