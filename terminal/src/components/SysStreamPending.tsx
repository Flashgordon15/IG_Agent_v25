"use client";

type Props = {
  active: boolean;
  className?: string;
};

export function SysStreamPending({ active, className = "" }: Props) {
  if (!active) return null;
  return (
    <div
      className={`absolute inset-0 z-20 flex items-center justify-center bg-[#050505]/92 ${className}`}
    >
      <span className="cq-mono text-[11px] font-bold tracking-widest text-[#ff9f1c]">
        [SYS_STREAM_PENDING]
      </span>
    </div>
  );
}
