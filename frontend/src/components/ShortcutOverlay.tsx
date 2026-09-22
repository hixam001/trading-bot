import { SHORTCUTS } from '../lib/shortcuts'

/**
 * §65 — the `?` help overlay: every real shortcut in the system, in the
 * palette's visual language (centered panel, fixed scrim, Esc or click
 * outside to dismiss). Rendered VERBATIM from the shared SHORTCUTS table the
 * dispatcher implements — the overlay cannot drift from actual bindings
 * because it is not a separate listing.
 */
export default function ShortcutOverlay({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 bg-base/80 flex items-center justify-center px-4"
      onMouseDown={onClose}
      data-testid="shortcut-overlay"
    >
      <div
        className="w-full max-w-[560px] bg-surface border border-line-strong rounded"
        role="dialog"
        aria-modal="true"
        aria-label="keyboard shortcuts"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-line">
          <h2 className="panel-title">keyboard shortcuts</h2>
        </div>
        <ul className="py-1" data-testid="shortcut-list">
          {SHORTCUTS.map((s) => (
            <li
              key={s.keys}
              className="px-4 py-2 flex items-baseline justify-between gap-3 text-xs"
            >
              <span className="font-mono text-bright whitespace-nowrap">{s.keys}</span>
              <span className="text-body text-right leading-snug">
                {s.what}
                <span className="block text-[10px] text-faint">{s.where}</span>
              </span>
            </li>
          ))}
        </ul>
        <div className="px-4 py-2 border-t border-line-soft text-[10px] text-faint">
          esc or click outside closes · read-only surface (no state writes)
        </div>
      </div>
    </div>
  )
}
