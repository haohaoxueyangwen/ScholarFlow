"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export function TranslationPopup({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  const [expanded, setExpanded] = useState(false);
  const popup = useRef<HTMLElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => setHost(document.body), 0);
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", escape);
    return () => { clearTimeout(timer); window.removeEventListener("keydown", escape); };
  }, [onClose]);
  useEffect(() => {
    if (!host) return;
    let frame = 0;
    const fit = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const element = popup.current;
        if (!element || !element.style.left) return;
        const rect = element.getBoundingClientRect();
        element.style.left = Math.max(8, Math.min(innerWidth - rect.width - 8, rect.left)) + "px";
        element.style.top = Math.max(8, Math.min(innerHeight - rect.height - 8, rect.top)) + "px";
      });
    };
    const observer = new ResizeObserver(fit);
    if (popup.current) observer.observe(popup.current);
    window.addEventListener("resize", fit);
    document.addEventListener("fullscreenchange", fit);
    fit();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); window.removeEventListener("resize", fit); document.removeEventListener("fullscreenchange", fit); };
  }, [host, expanded]);
  if (!host) return null;
  return createPortal(<section ref={popup} className={`reader-translation-popup${expanded ? " expanded" : ""}`} role="dialog" aria-modal="false" aria-label="选段翻译">
    <header onPointerDown={(event) => {
      if ((event.target as HTMLElement).closest("button")) return;
      const rect = popup.current!.getBoundingClientRect();
      drag.current = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
      event.currentTarget.setPointerCapture(event.pointerId);
    }} onPointerMove={(event) => {
      if (!drag.current || !popup.current) return;
      const left = Math.max(8, Math.min(innerWidth - popup.current.offsetWidth - 8, drag.current.left + event.clientX - drag.current.x));
      const top = Math.max(8, Math.min(innerHeight - popup.current.offsetHeight - 8, drag.current.top + event.clientY - drag.current.y));
      Object.assign(popup.current.style, { left: left + "px", top: top + "px", right: "auto", bottom: "auto" });
    }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
      <strong>选段翻译</strong><span>可拖动 · Esc 关闭</span><button aria-label={expanded ? "还原翻译浮窗" : "放大翻译浮窗"} onClick={() => setExpanded((value) => !value)}>{expanded ? "还原" : "放大"}</button><button aria-label="关闭翻译浮窗" onClick={onClose}>×</button>
    </header>
    <div className="reader-translation-content">{children}</div>
  </section>, host);
}

