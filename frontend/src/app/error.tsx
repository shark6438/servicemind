"use client";
import { TriangleAlert } from "lucide-react";
export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) { return <section className="page"><div className="fatal"><TriangleAlert aria-hidden="true" /><p className="eyebrow">界面异常</p><h1>当前视图没有完成渲染</h1><p>错误边界已阻止故障扩散。可重试当前视图，业务写操作不会自动重放。</p><button className="button button--primary" onClick={reset}>重新加载视图</button></div></section>; }
