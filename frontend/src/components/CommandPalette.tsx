import { useEffect, useMemo, useRef, useState } from 'react'
import type { JournalFilter, LivePositionRow } from '../types'

/**
 * Command palette (§63.1) — hand-rolled, deliberately: no new runtime
 * dependency (DESIGN.md §5) and this scope doesn't need cmdk's plugins.
 *
 * READ-ONLY by construction: view navigation, journal filtering and
 * jump-to-position only. The break-state toggle is deliberately absent — a
 * fuzzy-matched keystroke must never sit one slip away from live system
 * state (§63 safety constraint); state writes keep their explicit gated
 * paths.
 *
 * A11y: dialog + combobox semantics; Tab is inert while open (focus stays in
 * the input, so the trap is trivially correct); arrows/enter/esc operate it;
 * aria-activedescendant tracks the highlighted row.
 */

type Tab = 'dashboard' | 'holdings' | 'journal' | 'market' | 'system'

export interface PaletteJournalFilter {
  status: JournalFilter
  query: string
}

interface Item {
  id: string
  label: string
  hint: string
  run: () => void
}

export default function CommandPalette({
  open,
  onOpen,
  onClose,
  onGo,
  onJournal,
  onPosition,
  positions,
}: {
  open: boolean
  onOpen: () => void
  onClose: () => void
  onGo: (tab: Tab) => void
  onJournal: (f: PaletteJournalFilter) => void
  onPosition: (mint: string) => void
  positions: LivePositionRow[]
}) {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  // Global shortcuts: Cmd/Ctrl-K toggles the palette; Esc closes while open.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        if (open) onClose()
        else onOpen()
      } else if (open && e.key === 'Escape') {
        onClose()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onOpen, onClose])

  // Fresh state on every open; focus lands in the input (and stays there).
  useEffect(() => {
    if (open) {
      setQuery('')
      setActive(0)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open])

  const items = useMemo<Item[]>(() => {
    const list: Item[] = [
      { id: 'go-dashboard', label: 'go: live', hint: 'view', run: () => onGo('dashboard') },
      { id: 'go-holdings', label: 'go: holdings', hint: 'view', run: () => onGo('holdings') },
      { id: 'go-journal', label: 'go: journal', hint: 'view', run: () => onGo('journal') },
      { id: 'go-market', label: 'go: market', hint: 'view', run: () => onGo('market') },
      { id: 'go-system', label: 'go: system', hint: 'view', run: () => onGo('system') },
      { id: 'j-all', label: 'journal filter: show all', hint: 'journal', run: () => onJournal({ status: 'all', query: '' }) },
      { id: 'j-bound', label: 'journal filter: filled', hint: 'journal', run: () => onJournal({ status: 'bound', query: '' }) },
      { id: 'j-published', label: 'journal filter: memo only · no fill', hint: 'journal', run: () => onJournal({ status: 'published', query: '' }) },
      { id: 'j-failed', label: 'journal filter: failed', hint: 'journal', run: () => onJournal({ status: 'failed', query: '' }) },
    ]
    // Free text searches the journal by mint / symbol / exit reason.
    const q = query.trim()
    if (q) {
      list.push({
        id: 'j-search',
        label: `journal: search "${q}"`,
        hint: 'mint / reason',
        run: () => onJournal({ status: 'all', query: q }),
      })
    }
    for (const p of positions) {
      list.push({
        id: `pos-${p.mint_address}`,
        label: `position: ${p.symbol} ${p.mint_address}`,
        hint: 'open',
        run: () => onPosition(p.mint_address),
      })
    }
    return list
  }, [positions, query, onGo, onJournal, onPosition])

  // Substring match, case-insensitive; startswith ranks first. That is all.
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    const hit = items.filter((it) => it.label.toLowerCase().includes(q))
    return [
      ...hit.filter((it) => it.label.toLowerCase().startsWith(q)),
      ...hit.filter((it) => !it.label.toLowerCase().startsWith(q)),
    ]
  }, [items, query])

  useEffect(() => {
    if (active >= shown.length) setActive(0)
  }, [active, shown.length])

  function run(it: Item) {
    onClose()
    it.run()
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown' || (e.ctrlKey && e.key === 'n')) {
      e.preventDefault()
      setActive((a) => (shown.length ? (a + 1) % shown.length : 0))
    } else if (e.key === 'ArrowUp' || (e.ctrlKey && e.key === 'p')) {
      e.preventDefault()
      setActive((a) => (shown.length ? (a - 1 + shown.length) % shown.length : 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (shown[active]) run(shown[active])
    } else if (e.key === 'Tab') {
      // Focus trap: single-field dialog — Tab never leaves the palette.
      e.preventDefault()
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 bg-base/80 flex items-start justify-center px-4 pt-[12vh]"
      onMouseDown={onClose}
      data-testid="palette-overlay"
    >
      <div
        className="w-full max-w-[560px] bg-surface border border-line-strong rounded"
        role="dialog"
        aria-modal="true"
        aria-label="command palette"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-autocomplete="list"
          aria-activedescendant={shown.length > 0 ? `palette-opt-${active}` : undefined}
          placeholder="> go, filter, position"
          className="w-full bg-transparent px-4 py-3 font-mono text-sm text-bright outline-none border-b border-line"
          data-testid="palette-input"
        />
        <ul
          id="palette-list"
          role="listbox"
          aria-label="commands"
          className="max-h-[40vh] overflow-y-auto py-1"
        >
          {shown.map((it, i) => (
            <li
              key={it.id}
              id={`palette-opt-${i}`}
              role="option"
              aria-selected={it.id === (shown[active]?.id ?? '')}
              data-testid="palette-item"
              className={`px-4 py-2 text-xs flex items-baseline justify-between gap-3 cursor-pointer ${
                i === active ? 'bg-raised text-bright' : 'text-body'
              }`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault()
                run(it)
              }}
            >
              <span className="font-mono truncate">{it.label}</span>
              <span className="text-faint shrink-0">{it.hint}</span>
            </li>
          ))}
          {shown.length === 0 && (
            <li className="px-4 py-3 text-xs text-dim" role="option" aria-selected={false}>
              no matching command — "{query}"
            </li>
          )}
        </ul>
        <div className="px-4 py-2 border-t border-line-soft text-[10px] text-faint">
          arrows move · enter runs · esc closes · read-only palette (no state writes)
        </div>
      </div>
    </div>
  )
}
